"""
Copyright (c) 2022 Ruilong Li, UC Berkeley.
"""

from typing import Callable, List, Union
import numpy as np
import torch
from torch.autograd import Function
from torch.cuda.amp import custom_bwd, custom_fwd

try:
    import tinycudann as tcnn
except ImportError as e:
    print(
        f"Error: {e}! "
        "Please install tinycudann by: "
        "pip install git+https://github.com/NVlabs/tiny-cuda-nn/#subdirectory=bindings/torch"
    )
    exit()


class _TruncExp(Function):  # pylint: disable=abstract-method
    # Implementation from torch-ngp:
    # https://github.com/ashawkey/torch-ngp/blob/93b08a0d4ec1cc6e69d85df7f0acdfb99603b628/activation.py
    @staticmethod
    @custom_fwd(cast_inputs=torch.float32)
    def forward(ctx, x):  # pylint: disable=arguments-differ
        ctx.save_for_backward(x)
        return torch.exp(x)

    @staticmethod
    @custom_bwd
    def backward(ctx, g):  # pylint: disable=arguments-differ
        x = ctx.saved_tensors[0]
        return g * torch.exp(torch.clamp(x, max=15))


trunc_exp = _TruncExp.apply


def contract_to_unisphere(
    x: torch.Tensor,
    aabb: torch.Tensor,
    eps: float = 1e-6,
    derivative: bool = False,
):
    aabb_min, aabb_max = torch.split(aabb, 3, dim=-1)
    x = (x - aabb_min) / (aabb_max - aabb_min)
    x = x * 2 - 1  # aabb is at [-1, 1]
    mag = x.norm(dim=-1, keepdim=True)
    mask = mag.squeeze(-1) > 1

    if derivative:
        dev = (2 * mag - 1) / mag**2 + 2 * x**2 * (
            1 / mag**3 - (2 * mag - 1) / mag**4
        )
        dev[~mask] = 1.0
        dev = torch.clamp(dev, min=eps)
        return dev
    else:
        x[mask] = (2 - 1 / mag[mask]) * (x[mask] / mag[mask])
        x = x / 4 + 0.5  # [-inf, inf] is at [0, 1]
        return x


class NGPradianceField(torch.nn.Module):
    """Instance-NGP radiance Field"""

    def __init__(
        self,
        aabb: Union[torch.Tensor, List[float]],
        num_dim: int = 3,
        use_viewdirs: bool = False,
        cond_type: str = "none",
        density_activation: Callable = lambda x: trunc_exp(x - 1),
        unbounded: bool = False,
        geo_feat_dim: int = 15,
        n_levels: int = 16,
        log2_hashmap_size: int = 19,
    ) -> None:
        super().__init__()
        if not isinstance(aabb, torch.Tensor):
            aabb = torch.tensor(aabb, dtype=torch.float32)
        self.register_buffer("aabb", aabb)
        # self.aabb = aabb
        self.num_dim = num_dim
        self.use_viewdirs = use_viewdirs
        self.density_activation = density_activation
        self.unbounded = unbounded

        self.geo_feat_dim = geo_feat_dim
        # per_level_scale = 1.4472692012786865
        per_level_scale = float(np.exp2(np.log2(2048 * aabb[0].abs() / 16.) / (16. - 1.)))

        if self.use_viewdirs:
            if cond_type == "neck_pose":
                self.direction_encoding = tcnn.Encoding(
                    n_input_dims=num_dim,
                    encoding_config={
                        "otype": "Composite",
                        "nested": [
                            {
                                "n_dims_to_encode": 3,
                                "otype": "SphericalHarmonics",
                                "degree": 4,
                            },
                            # {"otype": "Identity", "n_bins": 4, "degree": 4},
                        ],
                    },
                )
            elif cond_type == "posed_verts":
                 self.direction_encoding = tcnn.Encoding(
                        n_input_dims=num_dim,
                        encoding_config={
                        # "otype": "SphericalHarmonics",
                        # "degree": 4,
                        "otype": "HashGrid",
                        "n_levels": n_levels//2,
                        "n_features_per_level": 2,
                        "log2_hashmap_size": log2_hashmap_size//2,
                        "base_resolution": 16,
                        "per_level_scale": per_level_scale,
                    },
                    )
            self.cond_type = cond_type
        self.mlp_base = tcnn.NetworkWithInputEncoding(
            n_input_dims=num_dim,
            n_output_dims=1 + self.geo_feat_dim,
            encoding_config={
                "otype": "HashGrid",
                "n_levels": n_levels,
                "n_features_per_level": 2,
                "log2_hashmap_size": log2_hashmap_size,
                "base_resolution": 16,
                "per_level_scale": per_level_scale,
            },
            network_config={
                "otype": "FullyFusedMLP",
                "activation": "ReLU",
                "output_activation": "None",
                "n_neurons": 64,
                "n_hidden_layers": 1,
            },
        )
        if self.geo_feat_dim > 0:
            self.mlp_head = tcnn.Network(
                n_input_dims=(
                    (
                        self.direction_encoding.n_output_dims
                        if self.use_viewdirs
                        else 0
                    )
                    + self.geo_feat_dim
                ),
                n_output_dims=3,
                network_config={
                    "otype": "FullyFusedMLP",
                    "activation": "ReLU",
                    "output_activation": "Sigmoid",
                    "n_neurons": 64,
                    "n_hidden_layers": 2,
                },
            )

    def query_density(self, x, return_feat: bool = False):
        ''' 
        x range: [-0.5, 0.5] 
        y range: [0.4, 0.7]
        z range: [-0.5, 0.3]
        '''
        if self.unbounded:
            x = contract_to_unisphere(x, self.aabb)
        else:
            # aabb = self.aabb.to(x.device)
            aabb_min, aabb_max = torch.split(self.aabb, self.num_dim, dim=-1)
            x = (x - aabb_min) / (aabb_max - aabb_min)
        selector = ((x > 0.0) & (x < 1.0)).all(dim=-1)
        x = (
            self.mlp_base(x.view(-1, self.num_dim))
            .view(list(x.shape[:-1]) + [1 + self.geo_feat_dim])
            .to(x)
        )
        density_before_activation, base_mlp_out = torch.split(
            x, [1, self.geo_feat_dim], dim=-1
        )
        density = (
            self.density_activation(density_before_activation)
            * selector[..., None]
        )
        if return_feat:
            return density, base_mlp_out
        else:
            return density

    def _query_rgb(self, dir, embedding):
        # tcnn requires directions in the range [0, 1]
        if self.use_viewdirs:
            if self.cond_type == "neck_pose":
                dir = (dir + 1.0) / 2.0
            elif self.cond_type == "posed_verts":
                aabb_min, aabb_max = torch.split(self.aabb, self.num_dim, dim=-1)
                dir = (dir - aabb_min) / (aabb_max - aabb_min)
            d = self.direction_encoding(dir.view(-1, dir.shape[-1]))
            h = torch.cat([d, embedding.view(-1, self.geo_feat_dim)], dim=-1)
        else:
            h = embedding.view(-1, self.geo_feat_dim)
        rgb = (
            self.mlp_head(h)
            .view(list(embedding.shape[:-1]) + [3])
            .to(embedding)
        )
        return rgb

    def forward(
        self,
        positions: torch.Tensor,
        directions: torch.Tensor = None,
    ):
        if self.use_viewdirs and (directions is not None):
            assert (
                positions.shape == directions.shape
            ), f"{positions.shape} v.s. {directions.shape}"
            density, embedding = self.query_density(positions, return_feat=True)
            rgb = self._query_rgb(directions, embedding=embedding)
        else:
            density, embedding = self.query_density(positions, return_feat=True)
            rgb = self._query_rgb(directions, embedding=embedding)
        return rgb, density


class NGPNet(torch.nn.Module):
    """Instance-NGP network"""

    def __init__(
        self,
        aabb: Union[torch.Tensor, List[float]],
        input_dim: int = 3, 
        cond_dim: int = 0,
        output_dim: int = 3,
        last_op: Callable = torch.sigmoid,
        scale: float = 1.0,
        log2_hashmap_size: int = 19,
        n_levels: int = 16,
    ) -> None:
        super().__init__()
        if not isinstance(aabb, torch.Tensor):
            aabb = torch.tensor(aabb, dtype=torch.float32)
        self.register_buffer("aabb", aabb)

        self.input_dim = input_dim
        self.output_dim = output_dim
        self.last_op = last_op
        self.scale = scale
        
        per_level_scale = float(np.exp2(np.log2(2048 * aabb[0].abs() / 16.) / (16. - 1.)))

        self.encoder = tcnn.Encoding(
            n_input_dims=input_dim,
            encoding_config={
                "otype": "HashGrid",
                "n_levels": n_levels,
                "n_features_per_level": 2,
                "log2_hashmap_size": log2_hashmap_size,
                "base_resolution": 16,
                "per_level_scale": per_level_scale,
            },
            )
        if cond_dim > 0:
            self.cond_encoder = tcnn.Encoding(
                n_input_dims=cond_dim,
                encoding_config={
                # "otype": "SphericalHarmonics",
                # "degree": 4,
                "otype": "HashGrid",
                "n_levels": n_levels//2,
                "n_features_per_level": 2,
                "log2_hashmap_size": log2_hashmap_size//2,
                "base_resolution": 16,
                "per_level_scale": per_level_scale,
            },
            )
        self.mlp = tcnn.Network(
            n_input_dims=self.encoder.n_output_dims + self.cond_encoder.n_output_dims if cond_dim > 0 else self.encoder.n_output_dims,
            n_output_dims=output_dim,
            network_config={
                "otype": "FullyFusedMLP",
                "activation": "ReLU",
                "output_activation": "None",
                "n_neurons": 64,
                "n_hidden_layers": 1,
            },
        )
        
    def forward(self, x, cond=None):
        ''' 
        x range: [-0.5, 0.5] 
        y range: [0.4, 0.7]
        z range: [-0.5, 0.3]
        '''
        aabb_min, aabb_max = torch.split(self.aabb, self.input_dim, dim=-1)
        x = (x - aabb_min) / (aabb_max - aabb_min)
        # selector = ((x > 0.0) & (x < 1.0)).all(dim=-1)
        x_enc = self.encoder(x.view(-1, self.input_dim))
        if cond is not None:
            cond = (cond - aabb_min) / (aabb_max - aabb_min)
            cond_enc = self.cond_encoder(cond.view(-1, cond.shape[-1]))
            x_enc = torch.cat([x_enc, cond_enc], dim=-1)     
        x = self.mlp(x_enc).view(list(x.shape[:-1]) + [self.output_dim]).to(x)
        x = self.last_op(x)*self.scale
        return x
    
    