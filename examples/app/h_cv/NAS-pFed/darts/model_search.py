# Copyright 2024 Ant Group Co., Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import torch
import torch.nn as nn
import torch.nn.functional as F

from darts.genotypes import PRIMITIVES, Genotype
from darts.operations import OPS, FactorizedReduce, ReLUConvBN
from darts.utils import count_parameters_in_MB


class MixedOp(nn.Module):

    def __init__(self, C, stride):
        super(MixedOp, self).__init__()
        self._ops = nn.ModuleList()
        for primitive in PRIMITIVES:
            op = OPS[primitive](C, stride, False)
            if "pool" in primitive:
                op = nn.Sequential(op, nn.BatchNorm2d(C, affine=False))
            self._ops.append(op)

    def forward(self, x, weights):
        # w is the operation mixing weights. see equation 2 in the original paper.
        return sum(w * op(x) for w, op in zip(weights, self._ops))


class Cell(nn.Module):

    def __init__(
        self, steps, multiplier, C_prev_prev, C_prev, C, reduction, reduction_prev
    ):
        super(Cell, self).__init__()
        self.reduction = reduction

        if reduction_prev:
            self.preprocess0 = FactorizedReduce(C_prev_prev, C, affine=False)
        else:
            self.preprocess0 = ReLUConvBN(C_prev_prev, C, 1, 1, 0, affine=False)
        self.preprocess1 = ReLUConvBN(C_prev, C, 1, 1, 0, affine=False)
        self._steps = steps
        self._multiplier = multiplier

        self._ops = nn.ModuleList()
        self._bns = nn.ModuleList()
        for i in range(self._steps):
            for j in range(2 + i):
                stride = 2 if reduction and j < 2 else 1
                op = MixedOp(C, stride)
                self._ops.append(op)

    def forward(self, s0, s1, weights):
        s0 = self.preprocess0(s0)
        s1 = self.preprocess1(s1)

        states = [s0, s1]
        offset = 0
        for i in range(self._steps):
            s = sum(
                self._ops[offset + j](h, weights[offset + j])
                for j, h in enumerate(states)
            )
            offset += len(states)
            states.append(s)
        return torch.cat(states[-self._multiplier :], dim=1)


class InnerCell(nn.Module):

    def __init__(
        self,
        steps,
        multiplier,
        C_prev_prev,
        C_prev,
        C,
        reduction,
        reduction_prev,
        weights,
    ):
        super(InnerCell, self).__init__()
        self.reduction = reduction

        if reduction_prev:
            self.preprocess0 = FactorizedReduce(C_prev_prev, C, affine=False)
        else:
            self.preprocess0 = ReLUConvBN(C_prev_prev, C, 1, 1, 0, affine=False)
        self.preprocess1 = ReLUConvBN(C_prev, C, 1, 1, 0, affine=False)
        self._steps = steps
        self._multiplier = multiplier

        self._ops = nn.ModuleList()
        self._bns = nn.ModuleList()
        # len(self._ops)=2+3+4+5=14
        offset = 0
        keys = list(OPS.keys())
        for i in range(self._steps):
            for j in range(2 + i):
                stride = 2 if reduction and j < 2 else 1
                weight = weights.data[offset + j]
                choice = keys[weight.argmax()]
                op = OPS[choice](C, stride, False)
                if "pool" in choice:
                    op = nn.Sequential(op, nn.BatchNorm2d(C, affine=False))
                self._ops.append(op)
            offset += i + 2

    def forward(self, s0, s1):
        s0 = self.preprocess0(s0)
        s1 = self.preprocess1(s1)

        states = [s0, s1]
        offset = 0
        for i in range(self._steps):
            s = sum(self._ops[offset + j](h) for j, h in enumerate(states))
            offset += len(states)
            states.append(s)

        return torch.cat(states[-self._multiplier :], dim=1)


class ModelForModelSizeMeasure(nn.Module):
    """
    This class is used only for calculating the size of the generated model.
    The choices of opeartions are made using the current alpha value of the DARTS model.
    The main difference between this model and DARTS model are the following:
        1. The __init__ takes one more parameter "alphas_normal" and "alphas_reduce"
        2. The new Cell module is rewriten to contain the functionality of both Cell and MixedOp
        3. To be more specific, MixedOp is replaced with a fixed choice of operation based on
            the argmax(alpha_values)
        4. The new Cell class is redefined as an Inner Class. The name is the same, so please be
            very careful when you change the code later
        5.

    """

    def __init__(
        self,
        C,
        num_classes,
        layers,
        criterion,
        alphas_normal,
        alphas_reduce,
        steps=4,
        multiplier=4,
        stem_multiplier=3,
    ):
        super(ModelForModelSizeMeasure, self).__init__()
        self._C = C
        self._num_classes = num_classes
        self._layers = layers
        self._criterion = criterion
        self._steps = steps
        self._multiplier = multiplier

        C_curr = stem_multiplier * C  # 3*16
        self.stem = nn.Sequential(
            nn.Conv2d(3, C_curr, 3, padding=1, bias=False), nn.BatchNorm2d(C_curr)
        )

        C_prev_prev, C_prev, C_curr = C_curr, C_curr, C
        self.cells = nn.ModuleList()
        reduction_prev = False

        # for layers = 8, when layer_i = 2, 5, the cell is reduction cell.
        for i in range(layers):
            if i in [layers // 3, 2 * layers // 3]:
                C_curr *= 2
                reduction = True
                cell = InnerCell(
                    steps,
                    multiplier,
                    C_prev_prev,
                    C_prev,
                    C_curr,
                    reduction,
                    reduction_prev,
                    alphas_reduce,
                )
            else:
                reduction = False
                cell = InnerCell(
                    steps,
                    multiplier,
                    C_prev_prev,
                    C_prev,
                    C_curr,
                    reduction,
                    reduction_prev,
                    alphas_normal,
                )

            reduction_prev = reduction
            self.cells += [cell]
            C_prev_prev, C_prev = C_prev, multiplier * C_curr

        self.global_pooling = nn.AdaptiveAvgPool2d(1)
        self.classifier = nn.Linear(C_prev, num_classes)

    def forward(self, input_data):
        s0 = s1 = self.stem(input_data)
        for i, cell in enumerate(self.cells):
            if cell.reduction:
                s0, s1 = s1, cell(s0, s1)
            else:
                s0, s1 = s1, cell(s0, s1)
        out = self.global_pooling(s1)
        logits = self.classifier(out.view(out.size(0), -1))
        return logits


class Network(nn.Module):

    def __init__(
        self,
        C,
        num_classes,
        layers,
        criterion,
        device,
        steps=4,
        multiplier=4,
        stem_multiplier=3,
    ):
        super(Network, self).__init__()
        print(Network)
        self._C = C
        self._num_classes = num_classes
        self._layers = layers
        self._criterion = criterion
        self._steps = steps
        self._multiplier = multiplier
        self._stem_multiplier = stem_multiplier

        self.device = device

        C_curr = stem_multiplier * C  # 3*16
        self.stem = nn.Sequential(
            nn.Conv2d(3, C_curr, 3, padding=1, bias=False), nn.BatchNorm2d(C_curr)
        )

        C_prev_prev, C_prev, C_curr = C_curr, C_curr, C
        self.cells = nn.ModuleList()
        reduction_prev = False

        # for layers = 8, when layer_i = 2, 5, the cell is reduction cell.
        for i in range(layers):
            if i in [layers // 3, 2 * layers // 3]:
                C_curr *= 2
                reduction = True
            else:
                reduction = False
            cell = Cell(
                steps,
                multiplier,
                C_prev_prev,
                C_prev,
                C_curr,
                reduction,
                reduction_prev,
            )
            reduction_prev = reduction
            self.cells += [cell]
            C_prev_prev, C_prev = C_prev, multiplier * C_curr

        self.global_pooling = nn.AdaptiveAvgPool2d(1)
        self.classifier = nn.Linear(C_prev, num_classes)

        self._initialize_alphas()

    def new(self):
        model_new = Network(
            self._C, self._num_classes, self._layers, self._criterion, self.device
        ).to(self.device)
        for x, y in zip(model_new.arch_parameters(), self.arch_parameters()):
            x.data.copy_(y.data)
        return model_new

    def forward(self, input):
        s0 = s1 = self.stem(input)
        for i, cell in enumerate(self.cells):
            if cell.reduction:
                weights = F.softmax(self.alphas_reduce, dim=-1)
            else:
                weights = F.softmax(self.alphas_normal, dim=-1)
            s0, s1 = s1, cell(s0, s1, weights)
        out = self.global_pooling(s1)
        logits = self.classifier(out.view(out.size(0), -1))
        return logits

    def _initialize_alphas(self):
        k = sum(1 for i in range(self._steps) for n in range(2 + i))
        num_ops = len(PRIMITIVES)

        self.alphas_normal = nn.Parameter(1e-3 * torch.randn(k, num_ops))
        self.alphas_reduce = nn.Parameter(1e-3 * torch.randn(k, num_ops))
        # self._arch_parameters = [
        #     self.alphas_normal,
        #     self.alphas_reduce,
        # ]
        self.history_normal = torch.zeros_like(self.alphas_normal)
        self.history_reduce = torch.zeros_like(self.alphas_reduce)

    def new_arch_parameters(self):
        k = sum(1 for i in range(self._steps) for n in range(2 + i))
        num_ops = len(PRIMITIVES)

        alphas_normal = nn.Parameter(1e-3 * torch.randn(k, num_ops)).to(self.device)
        alphas_reduce = nn.Parameter(1e-3 * torch.randn(k, num_ops)).to(self.device)
        _arch_parameters = [
            alphas_normal,
            alphas_reduce,
        ]
        return _arch_parameters

    def arch_parameters(self):
        # return self._arch_parameters
        return [self.alphas_normal, self.alphas_reduce]

    def genotype(self):
        def _isCNNStructure(k_best):
            return k_best >= 4

        def _parse(weights):
            gene = []
            n = 2
            start = 0
            cnn_structure_count = 0
            for i in range(self._steps):
                end = start + n
                W = weights[start:end].copy()
                edges = sorted(
                    range(i + 2),
                    key=lambda x: -max(
                        W[x][k]
                        for k in range(len(W[x]))
                        if k != PRIMITIVES.index("none")
                    ),
                )[:2]
                for j in edges:
                    k_best = None
                    for k in range(len(W[j])):
                        if k != PRIMITIVES.index("none"):
                            if k_best is None or W[j][k] > W[j][k_best]:
                                k_best = k

                    if _isCNNStructure(k_best):
                        cnn_structure_count += 1
                    gene.append((PRIMITIVES[k_best], j))
                start = end
                n += 1
            return gene, cnn_structure_count

        with torch.no_grad():
            gene_normal, cnn_structure_count_normal = _parse(
                F.softmax(self.alphas_normal, dim=-1).data.cpu().numpy()
            )
            gene_reduce, cnn_structure_count_reduce = _parse(
                F.softmax(self.alphas_reduce, dim=-1).data.cpu().numpy()
            )

            concat = range(2 + self._steps - self._multiplier, self._steps + 2)
            genotype = Genotype(
                normal=gene_normal,
                normal_concat=concat,
                reduce=gene_reduce,
                reduce_concat=concat,
            )
        return genotype, cnn_structure_count_normal, cnn_structure_count_reduce

    def get_current_model_size(self):
        model = ModelForModelSizeMeasure(
            self._C,
            self._num_classes,
            self._layers,
            self._criterion,
            self.alphas_normal,
            self.alphas_reduce,
            self._steps,
            self._multiplier,
            self._stem_multiplier,
        )
        size = count_parameters_in_MB(model)
        # This need to be further checked with cuda stuff
        del model
        return size


class EMNIST(nn.Module):

    # C：初始通道数，即网络第一层的通道数
    # num_classes：分类任务的类别数
    # layers：网络的层数，即cell的数量
    # criterion：损失函数，用于计算模型的损失
    # device：指定模型运行的设备
    # steps：每个cell内部的节点数
    # multiplier：用于指定cell的输出取有向无环图中的最后multiplier个通道
    # stem_multiplier：初始stem层的通道数乘数，用于调整stem层的输出通道数
    def __init__(
        self,
        C,
        num_classes,
        layers,
        criterion,
        device,
        steps=4,
        multiplier=4,
        stem_multiplier=3,
    ):
        super(EMNIST, self).__init__()
        print(Network)
        self._C = C
        self._num_classes = num_classes
        self._layers = layers
        self._criterion = criterion
        self._steps = steps
        self._multiplier = multiplier
        self._stem_multiplier = stem_multiplier

        self.device = device

        C_curr = stem_multiplier * C  # 3*16
        self.stem = nn.Sequential(
            nn.Conv2d(1, C_curr, 3, padding=1, bias=False), nn.BatchNorm2d(C_curr)
        )

        C_prev_prev, C_prev, C_curr = C_curr, C_curr, C
        self.cells = nn.ModuleList()
        reduction_prev = False

        # for layers = 8, when layer_i = 2, 5, the cell is reduction cell.
        # 虽然搜索时使用了layers个cell，但是可以从后面初始化alpha权重可以看出所有的Normal Cell共享相同的alpha权重，所有的Reduction Cell共享相同的alpha权重
        for i in range(layers):
            if i in [layers // 3, 2 * layers // 3]:
                # Reduction Cell的通道数翻倍，目的应该是为了提高模型的学习能力，这是在论文中没有提及的，论文中只提到了特征图的宽度和高度减半
                C_curr *= 2
                reduction = True
            else:
                reduction = False
            cell = Cell(
                steps,
                multiplier,
                C_prev_prev,
                C_prev,
                C_curr,
                reduction,
                reduction_prev,
            )
            reduction_prev = reduction
            self.cells += [cell]
            C_prev_prev, C_prev = C_prev, multiplier * C_curr

        self.global_pooling = nn.AdaptiveAvgPool2d(1)
        self.classifier = nn.Linear(C_prev, num_classes)

        self._initialize_alphas()

    def new(self):
        model_new = Network(
            self._C, self._num_classes, self._layers, self._criterion, self.device
        ).to(self.device)
        for x, y in zip(model_new.arch_parameters(), self.arch_parameters()):
            x.data.copy_(y.data)
        return model_new

    def forward(self, input):
        s0 = s1 = self.stem(input)
        for i, cell in enumerate(self.cells):
            if cell.reduction:
                weights = F.softmax(self.alphas_reduce, dim=-1)
            else:
                weights = F.softmax(self.alphas_normal, dim=-1)
            s0, s1 = s1, cell(s0, s1, weights)
        out = self.global_pooling(s1)
        logits = self.classifier(out.view(out.size(0), -1))
        return logits

    def _initialize_alphas(self):
        k = sum(1 for i in range(self._steps) for n in range(2 + i))
        num_ops = len(PRIMITIVES)

        self.alphas_normal = nn.Parameter(1e-3 * torch.randn(k, num_ops))
        self.alphas_reduce = nn.Parameter(1e-3 * torch.randn(k, num_ops))
        # self._arch_parameters = [
        #     self.alphas_normal,
        #     self.alphas_reduce,
        # ]
        # 保存历史的权重信息
        self.history_normal = torch.zeros_like(self.alphas_normal)
        self.history_reduce = torch.zeros_like(self.alphas_reduce)

    def new_arch_parameters(self):
        k = sum(1 for i in range(self._steps) for n in range(2 + i))
        num_ops = len(PRIMITIVES)

        # 初始化normal和reduce的每条边上各个操作的权重
        alphas_normal = nn.Parameter(1e-3 * torch.randn(k, num_ops)).to(self.device)
        alphas_reduce = nn.Parameter(1e-3 * torch.randn(k, num_ops)).to(self.device)
        _arch_parameters = [
            alphas_normal,
            alphas_reduce,
        ]
        return _arch_parameters

    def arch_parameters(self):
        # return self._arch_parameters
        return [self.alphas_normal, self.alphas_reduce]

    def genotype(self):
        # 检查输入边上的操作是否是卷积操作：k_best对应着genotypes.py文件中的PRIMITIVES数组，该数组中索引4及以后的位置对应的是卷积操作
        def _isCNNStructure(k_best):
            return k_best >= 4

        def _parse(weights):
            gene = []
            n = 2
            start = 0
            cnn_structure_count = 0
            # 循环处理Cell中的每个节点
            for i in range(self._steps):
                end = start + n
                # start表示当前节点所有边的起始索引，end表示终止索引。W仍然是一个二维矩阵，每一行代表该条边每种操作的权重
                W = weights[start:end].copy()
                # 先排序出权重值最大的一种操作做为每条边的操作，然后返回权重值最大的两条边作为当前节点本轮搜索的结果
                edges = sorted(
                    range(i + 2),
                    key=lambda x: -max(
                        W[x][k]
                        for k in range(len(W[x]))
                        if k != PRIMITIVES.index("none")
                    ),
                )[:2]
                for j in edges:
                    k_best = None
                    for k in range(len(W[j])):
                        if k != PRIMITIVES.index("none"):
                            if k_best is None or W[j][k] > W[j][k_best]:
                                k_best = k

                    if _isCNNStructure(k_best):
                        cnn_structure_count += 1
                    # PRIMITIVES[k_best]表示该边的操作类型，j表示该边输入节点的索引
                    gene.append((PRIMITIVES[k_best], j))
                start = end
                n += 1
            return gene, cnn_structure_count

        with torch.no_grad():
            # 先对行做softmax操作
            gene_normal, cnn_structure_count_normal = _parse(
                F.softmax(self.alphas_normal, dim=-1).data.cpu().numpy()
            )
            gene_reduce, cnn_structure_count_reduce = _parse(
                F.softmax(self.alphas_reduce, dim=-1).data.cpu().numpy()
            )

            concat = range(2 + self._steps - self._multiplier, self._steps + 2)
            genotype = Genotype(
                normal=gene_normal,
                normal_concat=concat,
                reduce=gene_reduce,
                reduce_concat=concat,
            )
        return genotype, cnn_structure_count_normal, cnn_structure_count_reduce

    def get_current_model_size(self):
        model = ModelForModelSizeMeasure(
            self._C,
            self._num_classes,
            self._layers,
            self._criterion,
            self.alphas_normal,
            self.alphas_reduce,
            self._steps,
            self._multiplier,
            self._stem_multiplier,
        )
        size = count_parameters_in_MB(model)
        # This need to be further checked with cuda stuff
        del model
        return size
