import torch
from shark.shark_inference import SharkInference
from torch.fx.experimental.proxy_tensor import make_fx
from torch._decomp import get_decompositions
import torch_mlir
import os


def _compile_module(args, shark_module, model_name, extra_args=[]):
    if args.load_vmfb or args.save_vmfb:
        extended_name = "{}_{}".format(model_name, args.device)
        vmfb_path = os.path.join(os.getcwd(), extended_name + ".vmfb")
        if args.load_vmfb and os.path.isfile(vmfb_path) and not args.save_vmfb:
            print("Loading flatbuffer from {}".format(vmfb_path))
            shark_module.load_module(vmfb_path)
        else:
            if args.save_vmfb:
                print("Saving to {}".format(vmfb_path))
            else:
                print(
                    "No vmfb found. Compiling and saving to {}".format(
                        vmfb_path
                    )
                )
            path = shark_module.save_module(
                os.getcwd(), extended_name, extra_args
            )
            shark_module.load_module(path)
    else:
        shark_module.compile(extra_args)
    return shark_module


# Downloads the model from shark_tank and returns the shark_module.
def get_shark_model(args, tank_url, model_name, extra_args=[]):
    from shark.shark_downloader import download_torch_model

    mlir_model, func_name, inputs, golden_out = download_torch_model(
        model_name, tank_url=tank_url
    )
    shark_module = SharkInference(
        mlir_model, func_name, device=args.device, mlir_dialect="linalg"
    )
    return _compile_module(args, shark_module, model_name, extra_args)


# Converts the torch-module into shark_module.
def compile_through_fx(args, model, inputs, model_name, extra_args=[]):

    fx_g = make_fx(
        model,
        decomposition_table=get_decompositions(
            [
                torch.ops.aten.embedding_dense_backward,
                torch.ops.aten.native_layer_norm_backward,
                torch.ops.aten.slice_backward,
                torch.ops.aten.select_backward,
                torch.ops.aten.norm.ScalarOpt_dim,
                torch.ops.aten.native_group_norm,
                torch.ops.aten.upsample_bilinear2d.vec,
                torch.ops.aten.split.Tensor,
                torch.ops.aten.split_with_sizes,
            ]
        ),
    )(*inputs)

    fx_g.graph.set_codegen(torch.fx.graph.CodeGen())
    fx_g.recompile()

    def strip_overloads(gm):
        """
        Modifies the target of graph nodes in :attr:`gm` to strip overloads.
        Args:
            gm(fx.GraphModule): The input Fx graph module to be modified
        """
        for node in gm.graph.nodes:
            if isinstance(node.target, torch._ops.OpOverload):
                node.target = node.target.overloadpacket
        gm.recompile()

    strip_overloads(fx_g)

    ts_g = torch.jit.trace(fx_g, inputs)

    module = torch_mlir.compile(
        ts_g,
        inputs,
        torch_mlir.OutputType.LINALG_ON_TENSORS,
        use_tracing=False,
        verbose=False,
    )

    mlir_model = module
    func_name = "forward"

    shark_module = SharkInference(
        mlir_model,
        func_name,
        device=args.device,
        mlir_dialect="linalg",
    )

    return _compile_module(args, shark_module, model_name, extra_args)
