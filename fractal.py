""" Fractal Model - per sample drop path """
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import utils


class Flatten(nn.Module):
    def forward(self, x):
        return x.view(x.size(0), -1)


class ConvBlock(nn.Module):
    """ Conv - Dropout - BN - ReLU """
    def __init__(self, C_in, C_out, kernel_size=3, stride=1, padding=1, dropout=None,
                 pad_type='zero'):
        super().__init__()

        if pad_type == 'zero':
            self.pad = nn.ZeroPad2d(padding)
        elif pad_type == 'reflect':
            # [!] the paper used reflect padding - just for data augmentation?
            self.pad = nn.ReflectionPad2d(padding)
        else:
            raise ValueError(pad_type)

        self.conv = nn.Conv2d(C_in, C_out, kernel_size, stride, padding=0, bias=False)
        if dropout is not None and dropout > 0.:
            self.dropout = nn.Dropout2d(p=dropout, inplace=True)
        else:
            self.dropout = None
        self.bn = nn.BatchNorm2d(C_out)

    def forward(self, x):
        out = self.pad(x)
        out = self.conv(out)
        if self.dropout:
            out = self.dropout(out)
        out = self.bn(out)
        out = F.relu_(out)

        return out


class FractalBlock(nn.Module):
    def __init__(self, n_columns, C_in, C_out, p_local_drop, p_dropout, pad_type='zero',
                 doubling=False):
        """ Fractal block
        Args:
            - n_columns: # of columns
            - C_in: channel_in
            - C_out: channel_out
            - p_local_drop: local droppath prob
            - p_dropout: dropout prob
            - pad_type: padding type of conv
            - doubling: if True, doubling by 1x1 conv in front of the block.
        """
        super().__init__()

        self.n_columns = n_columns
        self.columns = nn.ModuleList([nn.ModuleList() for _ in range(n_columns)])
        self.max_depth = 2 ** (n_columns-1)
        self.p_local_drop = p_local_drop

        if doubling:
            #self.doubler = nn.Conv2d(C_in, C_out, 1, padding=0)
            self.doubler = ConvBlock(C_in, C_out, 1, padding=0)
        else:
            self.doubler = None

        dist = self.max_depth
        self.count = np.zeros([self.max_depth], dtype=np.int)
        for col in self.columns:
            for i in range(self.max_depth):
                if (i+1) % dist == 0:
                    first_block = (i+1 == dist) # first block in this column
                    if first_block and not doubling:
                        # if doubling, always input channel size is C_out.
                        cur_C_in = C_in
                    else:
                        cur_C_in = C_out
                    module = ConvBlock(cur_C_in, C_out, dropout=p_dropout, pad_type=pad_type)
                    self.count[i] += 1
                else:
                    module = None

                col.append(module)

            dist //= 2

    def drop_mask(self, B, global_cols, n_cols):
        """ Generate drop mask; [n_cols, B].
        1) generate global masks
        2) generate local masks
        3) resurrect random path in all-dead column
        4) concat global and local masks

        Args:
            - B: batch_size
            - global_cols: global columns which to alive [GB]
            - n_cols: the number of columns of mask
        """
        # global drop mask
        GB = global_cols.shape[0]
        # calc gdrop cols / samples
        gdrop_cols = global_cols - (self.n_columns - n_cols)
        gdrop_indices = np.where(gdrop_cols >= 0)[0]
        # gen gdrop mask
        gdrop_mask = np.zeros([n_cols, GB], dtype=np.float32)
        gdrop_mask[gdrop_cols[gdrop_indices], gdrop_indices] = 1.

        # local drop mask
        LB = B - GB
        ldrop_mask = np.random.binomial(1, 1.-self.p_local_drop, [n_cols, LB]).astype(np.float32)
        alive_count = ldrop_mask.sum(axis=0)
        # resurrect all-dead case
        dead_indices = np.where(alive_count == 0.)[0]
        ldrop_mask[np.random.randint(0, n_cols, size=dead_indices.shape), dead_indices] = 1.

        drop_mask = np.concatenate((gdrop_mask, ldrop_mask), axis=1)
        device = utils.get_module_device(self)
        return torch.from_numpy(drop_mask).to(device)

    def join(self, outs, global_cols):
        """
        Args:
            - outs: the outputs to join
            - global_cols: global drop path columns
        """
        n_cols = len(outs)
        out = torch.stack(outs) # [n_cols, B, C, H, W]

        if self.training:
            masks = self.drop_mask(outs[0].size(0), global_cols, n_cols) # [n_cols, B]
            masks = masks.view(*masks.size(), 1, 1, 1) # unsqueeze to [n_cols, B, 1, 1, 1]
            n_alive = masks.sum(dim=0) # [B, 1, 1, 1]
            masked_out = out * masks # [n_cols, B, C, H, W]
            n_alive[n_alive == 0.] = 1. # all-dead cases
            out = masked_out.sum(dim=0) / n_alive # [B, C, H, W] / [B, 1, 1, 1]
        else:
            out = out.mean(dim=0) # no drop

        return out

    def forward(self, x, global_cols, deepest=False):
        out = self.doubler(x) if self.doubler else x
        outs = [out] * self.n_columns
        for i in range(self.max_depth):
            st = self.n_columns - self.count[i]
            cur_outs = [] # outs of current depth
            if deepest:
                st = self.n_columns - 1 # last column only

            for c in range(st, self.n_columns):
                cur_in = outs[c] # current input
                cur_module = self.columns[c][i] # current module
                cur_outs.append(cur_module(cur_in))

            # join
            #print("join in depth = {}, # of in_join = {}".format(i, len(cur_out)))
            joined = self.join(cur_outs, global_cols)

            for c in range(st, self.n_columns):
                outs[c] = joined

        return outs[0]


class FractalNet(nn.Module):
    def __init__(self, data_shape, n_columns, channels, p_local_drop, dropout_probs,
                 gdrop_ratio, gap=0, init='xavier', pad_type='zero', doubling=False,
                 consist_gdrop=False):
        """
        Args:
            - data_shape: (C, H, W, n_classes). e.g. (3, 32, 32, 10) - CIFAR 10.
            - n_columns: the number of columns
            - channels: channel outs (list)
            - p_local_drop: local drop prob
            - dropout_probs: dropout probs (list)
            - gdrop_ratio: global droppath ratio
        """
        super().__init__()
        self.B = len(channels) # the number of blocks
        self.consist_gdrop = consist_gdrop
        self.gdrop_ratio = gdrop_ratio
        self.n_columns = n_columns
        C_in, H, W, n_classes = data_shape
        assert len(channels) == len(dropout_probs)
        assert H == W
        size = H

        layers = nn.ModuleList()
        C_out = C_in # work like C_out of block0 == data channels.
        total_layers = 0
        for b, (C, p_dropout) in enumerate(zip(channels, dropout_probs)):
            C_in, C_out = C_out, C
            #print("Channel in = {}, Channel out = {}".format(C_in, C_out))
            fb = FractalBlock(n_columns, C_in, C_out, p_local_drop, p_dropout,
                              pad_type=pad_type, doubling=doubling)
            layers.append(fb)
            if gap == 0 or b < self.B-1:
                # Originally, every pool is max-pool in the paper (No GAP).
                layers.append(nn.MaxPool2d(2))
            elif gap == 1:
                # last layer and gap == 1
                layers.append(nn.AdaptiveAvgPool2d(1)) # average pooling

            size //= 2
            total_layers += fb.max_depth

        print("Last featuremap size = {}".format(size))
        print("Total layers = {}".format(total_layers))

        if gap == 2:
            layers.append(nn.Conv2d(channels[-1], 10, 1, padding=0)) # 1x1 conv
            layers.append(nn.AdaptiveAvgPool2d(1)) # gap
            layers.append(Flatten())
        else:
            layers.append(Flatten())
            layers.append(nn.Linear(channels[-1] * size * size, n_classes)) # fc layer

        self.layers = layers

        # initialization
        if init != 'default':
            initialize_ = {
                'xavier': nn.init.xavier_uniform_,
                'he': nn.init.kaiming_uniform_
            }[init]

            for n, p in self.named_parameters():
                if p.dim() > 1: # weights only
                    initialize_(p)
                else: # bn w/b or bias
                    if 'bn.weight' in n:
                        nn.init.ones_(p)
                    else:
                        nn.init.zeros_(p)

    def forward(self, x, deepest=False):
        GB = int(x.size(0) * self.gdrop_ratio)
        out = x
        global_cols = None
        for layer in self.layers:
            if isinstance(layer, FractalBlock):
                if not self.consist_gdrop or global_cols is None:
                    global_cols = np.random.randint(0, self.n_columns, size=[GB])

                out = layer(out, global_cols, deepest=deepest)
            else:
                out = layer(out)

        return out
