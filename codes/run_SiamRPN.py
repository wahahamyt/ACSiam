# --------------------------------------------------------
# DaSiamRPN
# Licensed under The MIT License
# Written by Qiang Wang (wangqiang2015 at ia.ac.cn)
# --------------------------------------------------------
import numpy as np
import torch
from torch.autograd import Variable
import torch.nn.functional as F
import visdom
import cv2
import PIL.Image as Image
import torchvision
import torchvision.transforms as T
from memory_profiler import profile # 内存占用分析插件
from update.updatenet import MatchingNetwork
viz = visdom.Visdom()
debug = True
from utils import get_subwindow_tracking, get_search_region_target, crop_image, overlap_ratio, shuffleTensor
import matplotlib.pyplot as plt

loader = T.Compose([T.ToTensor()]) 

def generate_anchor(total_stride, scales, ratios, score_size):
    anchor_num = len(ratios) * len(scales)
    anchor = np.zeros((anchor_num, 4),  dtype=np.float32)
    size = total_stride * total_stride
    count = 0
    for ratio in ratios:
        # ws = int(np.sqrt(size * 1.0 / ratio))
        ws = int(np.sqrt(size / ratio))
        hs = int(ws * ratio)
        for scale in scales:
            wws = ws * scale
            hhs = hs * scale
            anchor[count, 0] = 0
            anchor[count, 1] = 0
            anchor[count, 2] = wws
            anchor[count, 3] = hhs
            count += 1

    anchor = np.tile(anchor, score_size * score_size).reshape((-1, 4))
    ori = - (score_size / 2) * total_stride
    xx, yy = np.meshgrid([ori + total_stride * dx for dx in range(score_size)],
                         [ori + total_stride * dy for dy in range(score_size)])
    xx, yy = np.tile(xx.flatten(), (anchor_num, 1)).flatten(), \
             np.tile(yy.flatten(), (anchor_num, 1)).flatten()
    anchor[:, 0], anchor[:, 1] = xx.astype(np.float32), yy.astype(np.float32)
    return anchor


class TrackerConfig(object):
    # These are the default hyper-params for DaSiamRPN 0.3827
    windowing = 'cosine'  # to penalize large displacements [cosine/uniform]
    # Params from the network architecture, have to be consistent with the training
    exemplar_size = 127  # input z size
    instance_size = 271  # input x size (search region)
    total_stride = 8
    score_size = (instance_size-exemplar_size)/total_stride+1
    context_amount = 0.5  # context amount for the exemplar
    ratios = [0.33, 0.5, 1, 2, 3]
    scales = [8, ]
    anchor_num = len(ratios) * len(scales)
    anchor = []
    penalty_k = 0.055
    window_influence = 0.42
    lr = 0.295
    # adaptive change search region #
    adaptive = True

    def update(self, cfg):
        for k, v in cfg.items():
            setattr(self, k, v)
        self.score_size = (self.instance_size - self.exemplar_size) / self.total_stride + 1

# 计算目标的得分图
# @profile(precision=4, stream=open('memory_profiler.log', 'w+'))
def tracker_eval(im, avg_chans, net, x_crop, current_z, target_pos, target_sz, window, scale_z, p):

    def calc_score():
        delta, score = net(x_crop, current_z)
        # 用于边框回归的量,表示的是分别对5中anchor进行回归, 其结构展开来应该是(4, 5, 19, 19)
        delta = delta.permute(1, 2, 3, 0).contiguous().view(4, -1).data.cpu().numpy()
        # 目标打分的量, 表示的是对5种anchor分别处理下的打分的量,其结构展开来应该为(5, 19, 19)
        back_score = F.softmax(score.permute(1, 2, 3, 0).contiguous().view(2, -1), dim=0)
        score = back_score.data[1, :].cpu().numpy()
        b = score.reshape(5, 19, 19)
        viz.heatmap(b.mean(0), win="Pscore", opts={"title":"Pscore"})

        delta[0, :] = delta[0, :] * p.anchor[:, 2] + p.anchor[:, 0]
        delta[1, :] = delta[1, :] * p.anchor[:, 3] + p.anchor[:, 1]
        delta[2, :] = np.exp(delta[2, :]) * p.anchor[:, 2]
        delta[3, :] = np.exp(delta[3, :]) * p.anchor[:, 3]
        return delta, score, back_score

    def change(r):
        return np.maximum(r, 1./r)

    def sz(w, h):
        pad = (w + h) * 0.5
        sz2 = (w + pad) * (h + pad)
        return np.sqrt(sz2)

    def sz_wh(wh):
        pad = (wh[0] + wh[1]) * 0.5
        sz2 = (wh[0] + pad) * (wh[1] + pad)
        return np.sqrt(sz2)
    
    delta, score, back_score = calc_score()
    # size penalty
    s_c = change(sz(delta[2, :], delta[3, :]) / (sz_wh(target_sz)))  # scale penalty
    r_c = change((target_sz[0] / target_sz[1]) / (delta[2, :] / delta[3, :]))  # ratio penalty

    penalty = np.exp(-(r_c * s_c - 1.) * p.penalty_k)
    pscore = penalty * score

    # window float
    pscore = pscore * (1 - p.window_influence) + window * p.window_influence
   
    best_pscore_id = np.argmax(pscore)

    def calc_pos_sz(score_id):
        target = delta[:, score_id] / scale_z
        target_sz_new = target_sz / scale_z
        lr = penalty[score_id] * score[score_id] * p.lr

        res_x = target[0] + target_pos[0]
        res_y = target[1] + target_pos[1]

        res_w = target_sz_new[0] * (1 - lr) + target[2] * lr
        res_h = target_sz_new[1] * (1 - lr) + target[3] * lr

        target_pos_new = np.array([res_x, res_y])
        target_sz_new = np.array([res_w, res_h])
        return target_pos_new, target_sz_new

    target_pos_new, target_sz_new = calc_pos_sz(best_pscore_id)

    return target_pos_new, target_sz_new, score[best_pscore_id]

# 初始化跟踪器网络
def SiamRPN_init(im, target_pos_init, target_sz_init, net):
    state = dict()
    p = TrackerConfig()
    p.update(net.cfg)
    state['im_h'] = im.shape[0]
    state['im_w'] = im.shape[1]

    if p.adaptive:
        if ((target_sz_init[0] * target_sz_init[1]) / float(state['im_h'] * state['im_w'])) < 0.004:
            p.instance_size = 287  # small object big search region
        else:
            p.instance_size = 271

        p.score_size = (p.instance_size - p.exemplar_size) / p.total_stride + 1

    p.anchor = generate_anchor(p.total_stride, p.scales, p.ratios, int(p.score_size))
    # 计算图像3个通道的平均值
    avg_chans = np.mean(im, axis=(0, 1))

    wc_z = target_sz_init[0] + p.context_amount * sum(target_sz_init)
    hc_z = target_sz_init[1] + p.context_amount * sum(target_sz_init)
    s_z = round(np.sqrt(wc_z * hc_z))
    # initialize the exemplar
    z_crop = get_subwindow_tracking(im, target_pos_init, p.exemplar_size,
                                    s_z, avg_chans)
    z = Variable(z_crop.unsqueeze(0))

    state['p'] = p
    state['net'] = net
    state['avg_chans'] = avg_chans
    state['target_pos'] = target_pos_init
    state['target_sz'] = target_sz_init

    state['exemplar_size'] = p.exemplar_size
    state['s_z'] = s_z

    # 传入目标的位置和大小, 还有第一帧的模板
    net.temple(z)

    if p.windowing == 'cosine':
        window = np.outer(np.hanning(p.score_size), np.hanning(p.score_size))
    elif p.windowing == 'uniform':
        window = np.ones((p.score_size, p.score_size))
    window = np.tile(window.flatten(), p.anchor_num)
    state['window'] = window

    return state

# 跟踪
# @profile(precision=4, stream=open('memory_profiler.log', 'w+'))
def SiamRPN_track(state, im):
    p = state['p']
    net = state['net']
    avg_chans = state['avg_chans']
    window = state['window']
    target_pos_old = state['target_pos']
    target_sz_old = state['target_sz']

    wc_z = target_sz_old[1] + p.context_amount * sum(target_sz_old)
    hc_z = target_sz_old[0] + p.context_amount * sum(target_sz_old)
    s_z = np.sqrt(wc_z * hc_z)
    scale_z = p.exemplar_size / s_z
    d_search = (p.instance_size - p.exemplar_size) / 2
    pad = d_search / scale_z
    s_x = s_z + 2 * pad

    # extract scaled crops for search region x at previous target position
    # target_pos 表示的是前一帧的目标的位置
    x_crop_old = get_subwindow_tracking(im, target_pos_old, p.instance_size,
                                    round(s_x), avg_chans).unsqueeze(0)
    viz.image(x_crop_old.squeeze(), opts={"title":"search region"}, win="search region")

    current_z = get_subwindow_tracking(im, target_pos_old, p.exemplar_size, s_z, avg_chans)
    current_z = Variable(current_z.unsqueeze(0))

    # 这里的 target_pos 表示的是后一帧的目标新的位置
    target_pos_new, target_sz_new, score = tracker_eval(im, avg_chans, net, x_crop_old, current_z, target_pos_old, target_sz_old * scale_z, window, scale_z, p)

    # 得到新的exampler
    z_crop = get_subwindow_tracking(im, target_pos_new, p.exemplar_size, s_z, avg_chans)
    z = Variable(z_crop.unsqueeze(0))

    target_pos_new[0] = max(0, min(state['im_w'], target_pos_new[0]))
    target_pos_new[1] = max(0, min(state['im_h'], target_pos_new[1]))
    target_sz_new[0] = max(10, min(state['im_w'], target_sz_new[0]))
    target_sz_new[1] = max(10, min(state['im_h'], target_sz_new[1]))

    state['target_pos'] = target_pos_new
    state['target_sz'] = target_sz_new
    state['score'] = score
    return state