import math
import sys
import random
import time
import datetime
from typing import Iterable

import torch
import util.misc as utils

def augment(x, l, device, beta=0.5):
    mixs = []
    try:
        x=x.tensors
    except:
        pass
    mix = torch.distributions.beta.Beta(beta, beta).sample([x.shape[0], 1, 1, 1])
    mix = torch.maximum(mix, 1 - mix)
    mix = mix.to(device)
    mixs.append(mix)
    xmix = x * mix + torch.flip(x,(0,)) * (1 - mix)
    lmix = l * mix + torch.flip(l,(0,)) * (1 - mix)
    return xmix, lmix, mixs

def mix_targets(samples, targets, device):
    masks = [t["masks"] for t in targets]
    target_masks = torch.stack(masks)
    shp_y = target_masks.shape
    target_masks = target_masks.long()
    y_onehot = torch.zeros((shp_y[0], 5, shp_y[2], shp_y[3]))
    if target_masks.device.type == "cuda":
        y_onehot = y_onehot.cuda(target_masks.device.index)
    y_onehot.scatter_(1, target_masks, 1).float()
    target_masks = y_onehot
    aug_samples, aug_targets, rates = augment(samples, target_masks, device)
    return aug_samples, aug_targets, rates

# def mix_outputs(samples, outputs, device):
#     src_masks = outputs["pred_masks"]
#     src_masks = Func.softmax(src_masks, dim=1)
#     mix_samples, mix_outputs, rates = augment(samples, src_masks, device)
#     return mix_samples, mix_outputs, rates

# def mix_fixRates(outputs, rates, device):
#     src_masks = outputs["pred_masks"]
#     src_masks = Func.softmax(src_masks, dim=1)
#     if len(rates) == 1:
#         mix = rates[0]
#         lmix = src_masks * mix + torch.flip(src_masks,(0,)) * (1 - mix)
#     else:
#         print("len rates:", len(rates))
#     return lmix

def convert_targets(targets, device):
    masks = [t["masks"] for t in targets]
    target_masks = torch.stack(masks)
    shp_y = target_masks.shape
    target_masks = target_masks.long()
    y_onehot = torch.zeros((shp_y[0], 5, shp_y[2], shp_y[3]))
    if target_masks.device.type == "cuda":
        y_onehot = y_onehot.cuda(target_masks.device.index)
    y_onehot.scatter_(1, target_masks, 1).float()
    target_masks = y_onehot
    return target_masks

def train_one_epoch(model: torch.nn.Module, criterion: torch.nn.Module,
                    dataloader_dict: dict, optimizer: torch.optim.Optimizer,
                    device: torch.device, epoch: int, max_norm: float = 0):
    model.train()
    criterion.train()
    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    #metric_logger.add_meter('loss_multiDice', utils.SmoothedValue(window_size=1, fmt='{value:.2f}'))
    header = 'Epoch: [{}]'.format(epoch)
    print_freq = 10
    numbers = { k : len(v) for k, v in dataloader_dict.items() }
    iterats = { k : iter(v) for k, v in dataloader_dict.items() }
    tasks = dataloader_dict.keys()
    counts = { k : 0 for k in tasks }
    total_steps = sum(numbers.values())
    start_time = time.time()
    for step in range(total_steps):
        start = time.time()
        tasks = [ t for t in tasks if counts[t] < numbers[t] ]
        task = random.sample(tasks, 1)[0]
        samples, targets = next(iterats[task])
        counts.update({task : counts[task] + 1 })
        datatime = time.time() - start
        samples = samples.to(device)
        targets = [{k: v.to(device) for k, v in t.items() if not isinstance(v, str)} for t in targets]

        # mix samples and targets
        samples, targets, rates1 = mix_targets(samples, targets, device)
        ###

        ## original
        # targets_onehot= convert_targets(targets,device)
        ##

        outputs = model(samples, task)
        loss_dict = criterion(outputs, targets)
        weight_dict = criterion.weight_dict
        losses = sum(loss_dict[k] * weight_dict[k] for k in loss_dict.keys() if k in ['loss_CrossEntropy'])

        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = utils.reduce_dict(loss_dict)
        loss_dict_reduced_unscaled = { f'{k}_unscaled': v for k, v in loss_dict_reduced.items() }
        loss_dict_reduced_scaled = {k: v * weight_dict[k] for k, v in loss_dict_reduced.items() if k in ['loss_CrossEntropy']}
        losses_reduced_scaled = sum(loss_dict_reduced_scaled.values())

        loss_value = losses_reduced_scaled.item()

        if not math.isfinite(loss_value):
            print("Loss is {}, stopping training".format(loss_value))
            print(loss_dict_reduced)
            sys.exit(1)

        optimizer.zero_grad()
        losses.backward()
        optimizer.step()

        metric_logger.update(loss=loss_dict_reduced_scaled['loss_CrossEntropy'], **loss_dict_reduced_scaled)
        metric_logger.update(lr=optimizer.param_groups[0]["lr"])
        itertime = time.time() - start
        metric_logger.log_every(step, total_steps, datatime, itertime, print_freq, header)
    # gather the stats from all processes
    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('{} Total time: {} ({:.4f} s / it)'.format(header, total_time_str, total_time / total_steps))
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    stats = {k: meter.global_avg for k, meter in metric_logger.meters.items()}
    
    return stats


@torch.no_grad()
def evaluate(model, criterion, postprocessors, dataloader_dict, device, output_dir, visualizer, epoch, writer):
    model.eval()
    criterion.eval()

    metric_logger = utils.MetricLogger(delimiter="  ")
    #metric_logger.add_meter('loss_multiDice', utils.SmoothedValue(window_size=1, fmt='{value:.2f}'))
    header = 'Test:'

    print_freq = 10
    numbers = { k : len(v) for k, v in dataloader_dict.items() }
    iterats = { k : iter(v) for k, v in dataloader_dict.items() }
    tasks = dataloader_dict.keys()
    counts = { k : 0 for k in tasks }
    total_steps = sum(numbers.values())
    start_time = time.time()
    sample_list, output_list, target_list = [], [], []
    for step in range(total_steps):
        start = time.time()
        tasks = [ t for t in tasks if counts[t] < numbers[t] ] 
        task = random.sample(tasks, 1)[0]
        samples, targets = next(iterats[task])
        counts.update({task : counts[task] + 1 })
        datatime = time.time() - start
        samples = samples.to(device)
        targets = [{k: v.to(device) for k, v in t.items() if not isinstance(v, str)} for t in targets]

        targets_onehot= convert_targets(targets,device)
        outputs = model(samples.tensors, task)

        loss_dict = criterion(outputs, targets_onehot)
        weight_dict = criterion.weight_dict

        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = utils.reduce_dict(loss_dict)
        loss_dict_reduced_scaled = {k: v * weight_dict[k] for k, v in loss_dict_reduced.items() if k in weight_dict.keys()}
        loss_dict_reduced_unscaled = {f'{k}_unscaled': v for k, v in loss_dict_reduced.items()}
        metric_logger.update(loss=loss_dict_reduced_scaled['loss_CrossEntropy'], **loss_dict_reduced_scaled)
        itertime = time.time() - start
        metric_logger.log_every(step, total_steps, datatime, itertime, print_freq, header)
        if step % round(total_steps / 16.) == 0:  
            ##original  
            sample_list.append(samples.tensors[0])
            ##
            _, pre_masks = torch.max(outputs['pred_masks'][0], 0, keepdims=True)
            output_list.append(pre_masks)
            
            ##original
            target_list.append(targets[0]['masks'])
            ##
    # gather the stats from all processes
    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('{} Total time: {} ({:.4f} s / it)'.format(header, total_time_str, total_time / total_steps))
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger)
    stats = {k: meter.global_avg for k, meter in metric_logger.meters.items()}
    writer.add_scalar('avg_DSC', stats['Avg'], epoch)
    writer.add_scalar('avg_loss', stats['loss_CrossEntropy'], epoch)
    visualizer(torch.stack(sample_list), torch.stack(output_list), torch.stack(target_list), epoch, writer)
    
    return stats