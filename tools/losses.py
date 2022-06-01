import numpy as np
import torch
import torch.nn.functional as F
import torch.nn as nn
from tools.lovasz_losses import lovasz_softmax


class EdgeConv(nn.Module):
    def __init__(self):
        super(EdgeConv, self).__init__()
        self.conv_op = nn.Conv2d(1, 1, kernel_size=3, padding=1, bias=False)
        sobel_kernel = np.array([[-1, -1, -1], [-1, 8, -1], [-1, -1, -1]], dtype='float32')
        sobel_kernel = sobel_kernel.reshape((1, 1, 3, 3))
        self.conv_op.weight.data = torch.from_numpy(sobel_kernel)
    def forward(self, x):
        x = x.unsqueeze(1)
        out = self.conv_op(x)
        return out


def BoundaryLoss(prediction, label):
    cost = torch.nn.functional.mse_loss(
            prediction.float(),label.float())
    return torch.sum(cost)


def make_one_hot(labels, classes):
    one_hot = torch.FloatTensor(labels.size()[0], classes, labels.size()[2], labels.size()[3]).zero_().to(labels.device)
    target = one_hot.scatter_(1, labels.data, 1)
    return target


def get_weights(target):
    t_np = target.view(-1).data.cpu().numpy()

    classes, counts = np.unique(t_np, return_counts=True)
    cls_w = np.median(counts) / counts
    # cls_w = class_weight.compute_class_weight('balanced', classes, t_np)

    weights = np.ones(7)
    weights[classes] = cls_w
    return torch.from_numpy(weights).float().cuda()


class CrossEntropyLoss2d(nn.Module):
    def __init__(self, weight=None, ignore_index=255, reduction='mean'):
        super(CrossEntropyLoss2d, self).__init__()
        self.CE = nn.CrossEntropyLoss(weight=weight, ignore_index=ignore_index, reduction=reduction)

    def forward(self, output, target):
        loss = self.CE(output, target)
        return loss

class BCELoss(nn.Module):
    def __init__(self, smooth=1., ignore_index=255):
        super(BCELoss, self).__init__()
        self.ignore_index = ignore_index
        self.smooth = smooth
        self.BCE = nn.BCELoss()

    def forward(self, output, target):
        output = output.squeeze(1)
        loss = self.BCE(output, target.float())

        return loss


class BCE_BDLoss(nn.Module):
    def __init__(self, loss_weight):
        super(BCE_BDLoss, self).__init__()
        self.BCE = nn.BCELoss()
        self.loss_weight = loss_weight
        self.edge_conv = EdgeConv().cuda()

    def forward(self, output, target):
        output = output.squeeze(1)
        loss = self.BCE(output, target.float())
        output_bd = self.edge_conv(output)
        target_bd = self.edge_conv(target.float())
        loss_bd = BoundaryLoss(output_bd, target_bd)
        return loss*self.loss_weight[0]+loss_bd*self.loss_weight[1]


class DiceLoss(nn.Module):
    def __init__(self, smooth=1., ignore_index=255):
        super(DiceLoss, self).__init__()
        self.ignore_index = ignore_index
        self.smooth = smooth

    def forward(self, output, target):
        if self.ignore_index not in range(target.min(), target.max()):
            if (target == self.ignore_index).sum() > 0:
                target[target == self.ignore_index] = target.min()
        target = make_one_hot(target.unsqueeze(dim=1), classes=output.size()[1])
        output = F.softmax(output, dim=1)
        output_flat = output.contiguous().view(-1)
        target_flat = target.contiguous().view(-1)
        intersection = (output_flat * target_flat).sum()
        loss = 1 - ((2. * intersection + self.smooth) /
                    (output_flat.sum() + target_flat.sum() + self.smooth))
        return loss


class FocalLoss(nn.Module):
    def __init__(self, gamma=2, alpha=None, ignore_index=255, size_average=True):
        super(FocalLoss, self).__init__()
        self.gamma = gamma
        self.size_average = size_average
        self.CE_loss = nn.CrossEntropyLoss(reduce=False, ignore_index=ignore_index, weight=alpha)

    def forward(self, output, target):
        logpt = self.CE_loss(output, target)
        pt = torch.exp(-logpt)
        loss = ((1 - pt) ** self.gamma) * logpt
        if self.size_average:
            return loss.mean()
        return loss.sum()


class CE_DiceLoss(nn.Module):
    def __init__(self, smooth=1, reduction='mean', ignore_index=255, weight=None):
        super(CE_DiceLoss, self).__init__()
        self.smooth = smooth
        self.dice = DiceLoss()
        self.cross_entropy = nn.CrossEntropyLoss(weight=weight, reduction=reduction, ignore_index=ignore_index)

    def forward(self, output, target):
        CE_loss = self.cross_entropy(output, target)
        dice_loss = self.dice(output, target)
        return CE_loss + dice_loss


class TriLoss(nn.Module):
    def __init__(self, reduction='mean'):
        super(TriLoss, self).__init__()
        self.dice = DiceLoss()
        weights_f = torch.tensor([0.01, 1]).cuda().float()
        weights_b = torch.tensor([1, 0.01]).cuda().float()
        self.cef = nn.CrossEntropyLoss(weight=weights_f, reduction=reduction)
        self.ceb = nn.CrossEntropyLoss(weight=weights_b, reduction=reduction)
        self.BCE = nn.BCELoss()

    def forward(self, output, target):
        out, out_b, out_f = output
        out = out.squeeze(1)
        losses1 = self.BCE(out, target.float())  # calculate loss
        losses_f = self.cef(out_f, target)  # calculate loss
        losses_b = self.ceb(out_b, target)  # calculate loss
        losses = losses1 + losses_b * 0.3 + losses_f * 0.7
        return losses


class LovaszSoftmax(nn.Module):
    def __init__(self, classes='present', per_image=False, ignore_index=255):
        super(LovaszSoftmax, self).__init__()
        self.smooth = classes
        self.per_image = per_image
        self.ignore_index = ignore_index

    def forward(self, output, target):
        logits = F.softmax(output, dim=1)
        loss = lovasz_softmax(logits, target, ignore=self.ignore_index)
        return loss


def get_loss(loss_type, class_weights=None):
    if loss_type == 'ce':
        if class_weights is None:
            return CrossEntropyLoss2d()
        else:
            return CrossEntropyLoss2d(weight=class_weights)
    elif loss_type == 'DiceLoss':
        return DiceLoss()
    elif loss_type == 'FocalLoss':
        return FocalLoss()
    elif loss_type == 'CE_DiceLoss':
        return CE_DiceLoss()
    elif loss_type == 'LovaszSoftmax':
        return LovaszSoftmax()
    elif loss_type == 'bce':
        return BCELoss()
    elif loss_type == 'bce_bd':
        return BCE_BDLoss([0.7, 0.3])
    elif loss_type == 'triple':
        return TriLoss()
    else:
        return None
