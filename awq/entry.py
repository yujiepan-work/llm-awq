from lm_eval import evaluator, tasks
from transformers import AutoModelForCausalLM, AutoTokenizer, AutoConfig
import torch
import argparse
import os
import json
import numpy as np
from accelerate import init_empty_weights, infer_auto_device_map, dispatch_model, load_checkpoint_in_model
from accelerate.utils.modeling import get_balanced_memory
from awq.utils.parallel import auto_parallel
from awq.quantize.pre_quant import run_awq, apply_awq
from awq.quantize.quantizer import pseudo_quantize_model_weight, real_quantize_model_weight
from awq.utils.lm_eval_adaptor import LMEvalAdaptor
from awq.utils.utils import simple_dispatch_model
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--model_path', type=str, help='path of the hf model')
parser.add_argument('--batch_size', type=int, default=1, help='batch size')
parser.add_argument("--tasks", default=None, type=str)
parser.add_argument("--output_path", default=None, type=str, help='output txt path, by awq author')
parser.add_argument("--output_folder", default=None, type=str, help='folder to save all kinds of info, added by yujie')
parser.add_argument('--num_fewshot', type=int, default=0)
# model config
parser.add_argument('--parallel', action='store_true',
                    help="enable model parallelism")
# max memory to offload larger models to CPU
parser.add_argument('--max_memory', type=str, nargs='*',
                    help="List of device_id:max_memory pairs to be parsed into a dictionary; "
                    + "Example: 0:10GiB 1:10GiB cpu:30GiB; "
                    + "mode details here: "
                    + "https://huggingface.co/docs/accelerate/usage_guides/big_modeling")
parser.add_argument('--auto_parallel', action='store_true',
                    help="automatically set parallel and batch_size")
# quantization config
parser.add_argument('--w_bit', type=int, default=None)
parser.add_argument('--q_group_size', type=int, default=-1)
parser.add_argument('--no_zero_point', action='store_true',
                    help="disable zero_point")
parser.add_argument('--q_backend', type=str,
                    default="fake", choices=["fake", "real"])
# save/load real quantized weights
parser.add_argument('--dump_quant', type=str, default=None,
                    help='save quantized model')
parser.add_argument('--load_quant', type=str, default=None,
                    help='load quantized model')
# apply/save/load awq
parser.add_argument('--run_awq', action='store_true',
                    help="perform awq search process")
parser.add_argument('--dump_awq', type=str, default=None,
                    help="save the awq search results")
parser.add_argument('--load_awq', type=str, default=None,
                    help="load the awq search results")
parser.add_argument('--apply_sparse_mask', type=str, default=None,
                    help="apply the sparse masks before running awq")

# disable the auto_dispatch when loading the quantized model
parser.add_argument('--no_auto_dispatch', action='store_false', dest='auto_dispatch')
args = parser.parse_args()

max_memory = [v.split(':') for v in (args.max_memory or [])]
max_memory = {(int(k) if k.isdigit() else k): v for k, v in max_memory}

if args.auto_parallel:
    gpu_list = auto_parallel(args)

# get quantization config (apart from w_bit)
q_config = {
    "zero_point": not args.no_zero_point,  # by default True
    "q_group_size": args.q_group_size,  # whether to use group quantization

}
print("Quantization config:", q_config)


def to_encoded_array(tensor_or_array):
    if isinstance(tensor_or_array, torch.Tensor):
        array = tensor_or_array.cpu().contiguous().numpy()
    else:
        array = tensor_or_array
    shape = array.shape
    encoded_array = np.packbits(array.reshape(-1))
    return encoded_array, list(shape)


def from_encoded_array(encoded_array, shape):
    array = np.unpackbits(encoded_array)
    tensor = torch.from_numpy(array)
    return tensor[:np.prod(shape)].reshape(shape)


def apply_mask_to_model(model, mask_dict_path):
    import numpy as np
    mask_dict = torch.load(mask_dict_path)
    keys = list(mask_dict.keys())
    with torch.no_grad():
        for name, param in model.named_parameters():
            if name in mask_dict:
                print('Applying mask to', name)
                mask_tensor = from_encoded_array(*mask_dict[name])
                keys.remove(name)
                mask = mask_tensor.to(param.device)
                param.data[mask] = 0  # set weights to zero
        assert len(keys) == 0
    return model


# build model and tokenizer

def build_model_and_enc(model_path):
    if not os.path.exists(model_path):  # look into ssd
        print(f"{model_path} not found!")
    print(f"* Building model {model_path}")

    # all hf model
    config = AutoConfig.from_pretrained(model_path, trust_remote_code=True)
    if "mpt" in config.__class__.__name__.lower():
        enc = AutoTokenizer.from_pretrained(config.tokenizer_name, trust_remote_code=True)
    else:
        enc = AutoTokenizer.from_pretrained(model_path, use_fast=False, trust_remote_code=True)

    if args.load_quant:  # directly load quantized weights
        print("Loading pre-computed quantized weights...")
        with init_empty_weights():
            model = AutoModelForCausalLM.from_config(config=config,
                                                     torch_dtype=torch.float16, trust_remote_code=True)
        real_quantize_model_weight(
            model, w_bit=args.w_bit, q_config=q_config, init_only=True)

        model.tie_weights()

        # Infer device map
        kwargs = {"max_memory": max_memory} if len(max_memory) else {}
        device_map = infer_auto_device_map(
            model,
            no_split_module_classes=[
                "OPTDecoderLayer", "LlamaDecoderLayer", "BloomBlock", "MPTBlock", "DecoderLayer"],
            **kwargs
        )
        # Load checkpoint in the model
        load_checkpoint_in_model(
            model,
            checkpoint=args.load_quant,
            device_map=device_map,
            offload_state_dict=True,
        )
        # Dispatch model
        model = simple_dispatch_model(model, device_map=device_map)

        model.eval()
    else:  # fp16 to quantized
        args.run_awq &= not args.load_awq  # if load_awq, no need to run awq
        # Init model on CPU:
        kwargs = {"torch_dtype": torch.float16, "low_cpu_mem_usage": True}
        model = AutoModelForCausalLM.from_pretrained(
            model_path, config=config, trust_remote_code=True, **kwargs)
        if args.apply_sparse_mask is not None:
            print('Applying mask dict', args.apply_sparse_mask)
            model = apply_mask_to_model(model, args.apply_sparse_mask)

        model.eval()

        if args.run_awq:
            assert args.dump_awq, "Please save the awq results with --dump_awq"

            awq_results = run_awq(
                model, enc,
                w_bit=args.w_bit, q_config=q_config,
                n_samples=128, seqlen=512,
            )
            if args.dump_awq:
                dirpath = os.path.dirname(args.dump_awq)
                os.makedirs(dirpath, exist_ok=True)

                torch.save(awq_results, args.dump_awq)
                print("AWQ results saved at", args.dump_awq)

            exit(0)

        if args.load_awq:
            print("Loading pre-computed AWQ results from", args.load_awq)
            awq_results = torch.load(args.load_awq, map_location="cpu")
            apply_awq(model, awq_results)

        # weight quantization
        if args.w_bit is not None:
            if args.q_backend == "fake":
                assert args.dump_quant is None, \
                    "Need to use real quantization to dump quantized weights"
                _result = pseudo_quantize_model_weight(
                    model, w_bit=args.w_bit, q_config=q_config
                )
                if args.output_folder is not None:
                    Path(args.output_folder).mkdir(exist_ok=True, parents=True)
                    with open(Path(args.output_folder, 'quantized_sparsity.json'), 'w', encoding='utf-8') as f:
                        json.dump({
                            'sparsity_per_layer': _result['sparsity_per_layer'],
                            "model_sparsity": _result['model_sparsity'],
                        }, f)

            elif args.q_backend == "real":  # real quantization
                real_quantize_model_weight(
                    model, w_bit=args.w_bit, q_config=q_config
                )
                if args.dump_quant:
                    dirpath = os.path.dirname(args.dump_quant)
                    os.makedirs(dirpath, exist_ok=True)

                    print(
                        f"Saving the quantized model at {args.dump_quant}...")
                    torch.save(model.cpu().state_dict(), args.dump_quant)
                    exit(0)
            else:
                raise NotImplementedError

        if not args.auto_dispatch:
            return model, enc

        # Move the model to GPU (as much as possible) for LM evaluation
        kwargs = {"max_memory": get_balanced_memory(model, max_memory if len(max_memory) > 0 else None)}
        for key in list(kwargs['max_memory'].keys()):
            if str(key).isdigit():
                kwargs['max_memory'][key] = int(kwargs['max_memory'][key] * 0.8)
        device_map = infer_auto_device_map(
            model,
            # TODO: can we remove this?
            no_split_module_classes=[
                "OPTDecoderLayer", "LlamaDecoderLayer", "BloomBlock", "MPTBlock", "DecoderLayer"],
            **kwargs
        )
        print(device_map, flush=True)
        model = dispatch_model(model, device_map=device_map)

    return model, enc


def main():
    if args.output_path is not None and os.path.exists(args.output_path):
        # print(f"Results {args.output_path} already generated. Exit.")
        print(f"Results {args.output_path} already generated. Overwrite.")
        # exit()

    if args.dump_awq and os.path.exists(args.dump_awq):
        print(f"Found existing AWQ results {args.dump_awq}, exit.")
        exit()

    # a hack here to auto set model group
    model, enc = build_model_and_enc(args.model_path)

    if args.tasks is not None:
        task_names = args.tasks.split(",")

        lm_eval_model = LMEvalAdaptor(args.model_path, model, enc, args.batch_size)
        results = evaluator.simple_evaluate(
            model=lm_eval_model,
            tasks=task_names,
            batch_size=args.batch_size,
            no_cache=True,
            num_fewshot=args.num_fewshot,
        )

        print(evaluator.make_table(results))

        if args.output_path is not None:
            os.makedirs(os.path.dirname(args.output_path), exist_ok=True)
            # otherwise cannot save
            results["config"]["model"] = args.model_path
            with open(args.output_path, "w") as f:
                json.dump(results, f, indent=2)


if __name__ == '__main__':
    main()
