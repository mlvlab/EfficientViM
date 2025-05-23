# --------------------------------------------------------
# EfficientViT FPN Architecture for Downstream Tasks
# Copyright (c) 2022 Microsoft
# Adapted from mmdetection FPN and LightViT
#   mmdetection: (https://github.com/open-mmlab/mmdetection)
#   LightViT: (https://github.com/hunto/LightViT)
# Written by: Xinyu Liu
# --------------------------------------------------------
import warnings

import torch.nn as nn
import torch.nn.functional as F
from mmcv.cnn import ConvModule, xavier_init
from mmcv.runner import auto_fp16

from mmdet.models.builder import NECKS
from torch.nn import BatchNorm2d

from detection.efficientViM_utils import LayerNorm2D


@NECKS.register_module()
class EfficientViTFPN(nn.Module):
    r"""Feature Pyramid Network for EfficientViT.
    Args:
        in_channels (List[int]): Number of input channels per scale.
        out_channels (int): Number of output channels (used at each scale)
        num_outs (int): Number of output scales.
        start_level (int): Index of the start input backbone level used to
            build the feature pyramid. Default: 0.
        end_level (int): Index of the end input backbone level (exclusive) to
            build the feature pyramid. Default: -1, which means the last level.
        add_extra_convs (bool | str): If bool, it decides whether to add conv
            layers on top of the original feature maps. Default to False.
            If True, its actual mode is specified by `extra_convs_on_inputs`.
            If str, it specifies the source feature map of the extra convs.
            Only the following options are allowed
            - 'on_input': Last feat map of neck inputs (i.e. backbone feature).
            - 'on_lateral':  Last feature map after lateral convs.
            - 'on_output': The last output feature map after fpn convs.
        extra_convs_on_inputs (bool, deprecated): Whether to apply extra convs
            on the original feature from the backbone. If True,
            it is equivalent to `add_extra_convs='on_input'`. If False, it is
            equivalent to set `add_extra_convs='on_output'`. Default to True.
        relu_before_extra_convs (bool): Whether to apply relu before the extra
            conv. Default: False.
        no_norm_on_lateral (bool): Whether to apply norm on lateral.
            Default: False.
        num_extra_trans_convs (int): extra transposed conv on the output 
            with largest resolution. Default: 0.
        conv_cfg (dict): Config dict for convolution layer. Default: None.
        norm_cfg (dict): Config dict for normalization layer. Default: None.
        act_cfg (str): Config dict for activation layer in ConvModule.
            Default: None.
        upsample_cfg (dict): Config dict for interpolate layer.
            Default: `dict(mode='nearest')`
    """

    def __init__(self,
                 in_channels,
                 out_channels,
                 num_outs,
                 start_level=0,
                 end_level=-1,
                 add_extra_convs=False,
                 extra_convs_on_inputs=True,
                 relu_before_extra_convs=False,
                 no_norm_on_lateral=False,
                 num_extra_trans_convs=0,
                 pre_norm = False,
                 conv_cfg=None,
                 norm_cfg=None,
                 act_cfg=None,
                 upsample_cfg=dict(mode='nearest')):
        super(EfficientViTFPN, self).__init__()
        assert isinstance(in_channels, list)
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.num_ins = len(in_channels)
        self.num_outs = num_outs
        self.relu_before_extra_convs = relu_before_extra_convs
        self.no_norm_on_lateral = no_norm_on_lateral
        self.num_extra_trans_convs = num_extra_trans_convs
        self.fp16_enabled = False
        self.upsample_cfg = upsample_cfg.copy()

        if end_level == -1:
            self.backbone_end_level = self.num_ins
            assert num_outs >= self.num_ins - start_level
        else:
            # if end_level < inputs, no extra level is allowed
            self.backbone_end_level = end_level
            assert end_level <= len(in_channels)
            assert num_outs == end_level - start_level
        self.start_level = start_level
        self.end_level = end_level
        self.add_extra_convs = add_extra_convs
        assert isinstance(add_extra_convs, (str, bool))
        if isinstance(add_extra_convs, str):
            # Extra_convs_source choices: 'on_input', 'on_lateral', 'on_output'
            assert add_extra_convs in ('on_input', 'on_lateral', 'on_output')
        elif add_extra_convs:  # True
            if extra_convs_on_inputs:
                # TODO: deprecate `extra_convs_on_inputs`
                warnings.simplefilter('once')
                warnings.warn(
                    '"extra_convs_on_inputs" will be deprecated in v2.9.0,'
                    'Please use "add_extra_convs"', DeprecationWarning)
                self.add_extra_convs = 'on_input'
            else:
                self.add_extra_convs = 'on_output'

        self.lateral_convs = nn.ModuleList()
        self.fpn_convs = nn.ModuleList()
        self.batch_norm = nn.ModuleList()
        self.pre_norm = pre_norm

        for i in range(self.start_level, self.backbone_end_level):
            if self.pre_norm:
                self.batch_norm.append(LayerNorm2D(in_channels[i]))
                # self.batch_norm.append(BatchNorm2d(in_channels[i]))
            l_conv = ConvModule(
                in_channels[i],
                out_channels,
                1,
                conv_cfg=conv_cfg,
                norm_cfg=norm_cfg if not self.no_norm_on_lateral else None,
                act_cfg=act_cfg,
                inplace=False)
            fpn_conv = ConvModule(
                out_channels,
                out_channels,
                3,
                padding=1,
                conv_cfg=conv_cfg,
                norm_cfg=norm_cfg,
                act_cfg=act_cfg,
                inplace=False)

            self.lateral_convs.append(l_conv)
            self.fpn_convs.append(fpn_conv)

        # add extra conv layers (e.g., RetinaNet)
        extra_levels = num_outs - self.backbone_end_level + self.start_level
        # 5 - 4 + 0 = 1
        # 5 - 3 + 0 = 2
        assert extra_levels >= num_extra_trans_convs
        extra_levels -= num_extra_trans_convs
        if self.add_extra_convs and extra_levels >= 1:
            for i in range(extra_levels):
                if i == 0 and self.add_extra_convs == 'on_input':
                    in_channels = self.in_channels[self.backbone_end_level - 1]
                else:
                    in_channels = out_channels
                extra_fpn_conv = ConvModule(
                    in_channels,
                    out_channels,
                    3,
                    stride=2,
                    padding=1,
                    conv_cfg=conv_cfg,
                    norm_cfg=norm_cfg,
                    act_cfg=act_cfg,
                    inplace=False)
                self.fpn_convs.append(extra_fpn_conv)
        
        # add extra transposed convs
        self.extra_trans_convs = nn.ModuleList()
        self.extra_fpn_convs = nn.ModuleList()

        for i in range(num_extra_trans_convs):
            extra_trans_conv = TransposedConvModule(
                out_channels,
                out_channels,
                2,
                stride=2,
                padding=0,
                conv_cfg=conv_cfg,
                norm_cfg=norm_cfg if not no_norm_on_lateral else None,
                act_cfg=act_cfg,
                inplace=False)
            self.extra_trans_convs.append(extra_trans_conv)
            extra_fpn_conv = ConvModule(
                out_channels,
                out_channels,
                3,
                padding=1,
                conv_cfg=conv_cfg,
                norm_cfg=norm_cfg,
                act_cfg=act_cfg,
                inplace=False)
            self.extra_fpn_convs.append(extra_fpn_conv)

    # default init_weights for conv(msra) and norm in ConvModule
    def init_weights(self):
        """Initialize the weights of FPN module."""
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                xavier_init(m, distribution='uniform')

    @auto_fp16()
    def forward(self, inputs):
        """Forward function."""
        assert len(inputs) == len(self.in_channels)
        if self.pre_norm: inputs = [norm(inputs[i]) for i, norm in enumerate(self.batch_norm)]
        # build laterals
        laterals = [
            lateral_conv(inputs[i + self.start_level])
            for i, lateral_conv in enumerate(self.lateral_convs)
        ]

        # build top-down path
        used_backbone_levels = len(laterals)
        for i in range(used_backbone_levels - 1, 0, -1):
            # In some cases, fixing `scale factor` (e.g. 2) is preferred, but
            #  it cannot co-exist with `size` in `F.interpolate`.
            if 'scale_factor' in self.upsample_cfg:
                laterals[i - 1] += F.interpolate(laterals[i],
                                                 **self.upsample_cfg)
            else:
                prev_shape = laterals[i - 1].shape[2:]
                laterals[i - 1] += F.interpolate(
                    laterals[i], size=prev_shape, **self.upsample_cfg)

        # extra transposed convs for outputs with extra scales
        extra_laterals = []
        if self.num_extra_trans_convs > 0:
            prev_lateral = laterals[0]
            for i in range(self.num_extra_trans_convs):
                extra_lateral = self.extra_trans_convs[i](prev_lateral)
                extra_laterals.insert(0, extra_lateral)
                prev_lateral = extra_lateral
                
        # part 1: from original levels
        outs = [
            self.fpn_convs[i](laterals[i]) for i in range(used_backbone_levels)
        ]
        
        # part 2: add extra levels
        if self.num_outs > len(outs) + len(extra_laterals):
            # use max pool to get more levels on top of outputs
            # (e.g., Faster R-CNN, Mask R-CNN)
            if not self.add_extra_convs:
                for i in range(self.num_outs - len(extra_laterals) - used_backbone_levels):
                    outs.append(F.max_pool2d(outs[-1], 1, stride=2))
            # add conv layers on top of original feature maps (RetinaNet)
            else:
                if self.add_extra_convs == 'on_input':
                    extra_source = inputs[self.backbone_end_level - 1]
                elif self.add_extra_convs == 'on_lateral':
                    extra_source = laterals[-1]
                elif self.add_extra_convs == 'on_output':
                    extra_source = outs[-1]
                else:
                    raise NotImplementedError
                outs.append(self.fpn_convs[used_backbone_levels](extra_source))

                for i in range(used_backbone_levels + 1, self.num_outs - len(extra_laterals)): # Not called
                    print("i: {}".format(i), self.fpn_convs[i])
                    if self.relu_before_extra_convs:
                        outs.append(self.fpn_convs[i](F.relu(outs[-1])))
                    else:
                        outs.append(self.fpn_convs[i](outs[-1]))
      
        # part 3: add extra transposed convs
        if self.num_extra_trans_convs > 0:
            # apply 3x3 conv on the larger feat (1/8) after 3x3 trans conv
            # because the 3x3 trans conv is on the lateral
            # thus no extra 1x1 laterals are required
            extra_outs = [
                self.extra_fpn_convs[i](extra_laterals[i]) 
                    for i in range(self.num_extra_trans_convs)
            ]
            # 1 + 4 (3+1extra) = 5
        assert (len(extra_outs) + len(outs)) == self.num_outs, f"{len(extra_outs)} + {len(outs)} != {self.num_outs}"
        return tuple(extra_outs + outs)


class TransposedConvModule(ConvModule):
    def __init__(self, in_channels, out_channels, kernel_size, stride=1, 
                 padding=0, dilation=1, groups=1, bias='auto', conv_cfg=None, 
                 norm_cfg=None, act_cfg=..., inplace=True, 
                 **kwargs):
        super(TransposedConvModule, self).__init__(in_channels, out_channels, kernel_size, 
                         stride, padding, dilation, groups, bias, conv_cfg, 
                         norm_cfg, act_cfg, inplace, **kwargs)
        
        self.conv = nn.ConvTranspose2d(
            in_channels,
            out_channels,
            kernel_size,
            stride=stride,
            padding=padding,
            dilation=dilation,
            groups=groups,
            bias=self.with_bias
        )

        # Use msra init by default
        self.init_weights()