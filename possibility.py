##################################################
# Copyright (c) Xuanyi Dong [GitHub D-X-Y], 2019 #
######################################################################################
import os, sys, time, glob, random, argparse
import numpy as np
from copy import deepcopy
import torch
import torch.nn as nn
from pathlib import Path
lib_dir = (Path("__file__").parent / 'lib').resolve()
if str(lib_dir) not in sys.path: sys.path.insert(0, str(lib_dir))
from config_utils import load_config, dict2config
from datasets     import get_datasets, get_nas_search_loaders
from procedures   import prepare_seed, prepare_logger, save_checkpoint, copy_checkpoint, get_optim_scheduler
from procedures.transfer import get_search_methods
from utils        import get_model_infos, obtain_accuracy
from log_utils    import AverageMeter, time_string, convert_secs2time, write_results
from models       import get_cell_based_tiny_net, get_search_spaces, load_net_from_checkpoint, FeatureMatching, CellStructure as Structure
from nas_201_api  import NASBench201API as API
from collections import OrderedDict

def get_n_archs(data, n, sample_method="top", order=True):
    """Get top n players by score.
    Returns a dictionary or an `OrderedDict` if `order` is true.
    """
    if sample_method == "top":
        subset = sorted(data.items(), key=lambda x: x[1]['accuracy'], reverse=True)[:n]
    elif sample_method == "fair":
        assert n == 4, "Currently, n must be 4."
        p1 = '|nor_conv_3x3~0|+|nor_conv_3x3~0|nor_conv_3x3~1|+|skip_connect~0|nor_conv_3x3~1|nor_conv_1x1~2|'
        p2 = '|nor_conv_1x1~0|+|nor_conv_1x1~0|nor_conv_1x1~1|+|none~0|nor_conv_1x1~1|nor_conv_3x3~2|'
        p3 = '|nor_conv_3x3~0|+|nor_conv_1x1~0|nor_conv_3x3~1|+|none~0|nor_conv_3x3~1|nor_conv_3x3~2|'
        p4 = '|nor_conv_1x1~0|+|nor_conv_3x3~0|nor_conv_1x1~1|+|skip_connect~0|nor_conv_1x1~1|nor_conv_1x1~2|'
        paths = [p1, p2, p3, p4]
        subset = filter(lambda x: x[1]["arch_str"] in paths, data.items())
    else:
        rand_indicies = random.sample(range(len(data)), n)
        subset = filter(lambda x: x[0] in rand_indicies, data.items())

    if order:
        return OrderedDict(subset)
    else:
        return dict(subset)

def list_arch(api, dataset, metric_on_set, FLOP_max=None, Param_max=None, use_12epochs_result=False):
    """Find the architecture with the highest accuracy based on some constraints."""
    if use_12epochs_result: basestr, arch2infos = '12epochs' , api.arch2infos_less
    else                  : basestr, arch2infos = '200epochs', api.arch2infos_full
    result = OrderedDict()
    for i, arch_id in enumerate(api.evaluated_indexes):
      info = arch2infos[arch_id].get_compute_costs(dataset)
      flop, param, latency = info['flops'], info['params'], info['latency']
      if FLOP_max  is not None and flop  > FLOP_max : continue
      if Param_max is not None and param > Param_max: continue
      xinfo = arch2infos[arch_id].get_metrics(dataset, metric_on_set)
      loss, accuracy = xinfo['loss'], xinfo['accuracy']
      arch_str = api.query_by_index(arch_id).arch_str
      result[arch_id] = { "arch_str": arch_str, "accuracy": accuracy, "flop": flop, "param": param }
    return result

def get_lr(optimizer):
    for param_group in optimizer.param_groups:
        return param_group['lr']

def get_best_arch(xloader, network, n_samples):
  # setn evaluation
  with torch.no_grad():
    network.eval()
    archs, valid_accs = network.module.return_topK(n_samples), []
    #print ('obtain the top-{:} architectures'.format(n_samples))
    loader_iter = iter(xloader)
    for i, sampled_arch in enumerate(archs):
      network.module.set_cal_mode('dynamic', sampled_arch)
      try:
        inputs, targets = next(loader_iter)
      except:
        loader_iter = iter(xloader)
        inputs, targets = next(loader_iter)

      _, logits = network(inputs)
      val_top1, val_top5 = obtain_accuracy(logits.cpu().data, targets.data, topk=(1, 5))

      valid_accs.append( val_top1.item() )
      #print ('--- {:}/{:} : {:} : {:}'.format(i, len(archs), sampled_arch, val_top1))

    best_idx = np.argmax(valid_accs)
    best_arch, best_valid_acc = archs[best_idx], valid_accs[best_idx]
    return best_arch, best_valid_acc

def search_w_setn(xloader, network, criterion, scheduler, w_optimizer, epoch_str, print_freq, logger, search_scope=None):
  data_time, batch_time = AverageMeter(), AverageMeter()
  base_losses = AverageMeter()
  base_top1, base_top5 = AverageMeter(), AverageMeter()
  end = time.time()
  network.train()
  for step, (base_inputs, base_targets, _, _) in enumerate(xloader):
    scheduler.update(None, 1.0 * step / len(xloader))
    base_targets = base_targets.cuda(non_blocking=True)
    # measure data loading time
    data_time.update(time.time() - end)
    # update the weights
    if search_scope is None:
        sampled_arch = network.module.dync_genotype(True) # uniform sampling
        #network.module.set_cal_mode( 'urs' )
    else:
        arch_info = random.sample(search_scope.items(), 1)[0] # ( arch_id, {arch_str, accuracy, flop, param} )
        arch_str = arch_info[1]['arch_str']
        sampled_arch = Structure(API.str2lists(arch_str))
    network.module.set_cal_mode('dynamic', sampled_arch)
    network.zero_grad()
    _, logits, st_outs = network(base_inputs, out_all=True)
    base_loss = criterion(logits, base_targets)
    base_loss.backward()
    w_optimizer.step()
    # record
    base_prec1, base_prec5 = obtain_accuracy(logits.data, base_targets.data, topk=(1, 5))
    base_top1.update  (base_prec1.item(), base_inputs.size(0))
    base_top5.update  (base_prec5.item(), base_inputs.size(0))
    base_losses.update(base_loss.item(),  base_inputs.size(0))
    # measure elapsed time
    batch_time.update(time.time() - end)
    end = time.time()
    if step % print_freq == 0 or step + 1 == len(xloader):
      Sstr = '*SEARCH w* ' + time_string() + ' [{:}][{:03d}/{:03d}]'.format(epoch_str, step, len(xloader))
      Tstr = 'Time {batch_time.val:.2f} ({batch_time.avg:.2f}) Data {data_time.val:.2f} ({data_time.avg:.2f})'.format(batch_time=batch_time, data_time=data_time)
      Wstr = 'Base [Loss {loss.val:.3f} ({loss.avg:.3f})  Prec@1 {top1.val:.2f} ({top1.avg:.2f}) Prec@5 {top5.val:.2f} ({top5.avg:.2f})]'.format(loss=base_losses, top1=base_top1, top5=base_top5)
      logger.log(Sstr + ' ' + Tstr + ' ' + Wstr)
  return base_losses.avg, base_top1.avg, base_top5.avg

def search_a_setn(xloader, network, criterion, a_optimizer, epoch_str, print_freq, logger):
  data_time, batch_time = AverageMeter(), AverageMeter()
  arch_losses = AverageMeter()
  arch_top1, arch_top5 = AverageMeter(), AverageMeter()
  end = time.time()
  network.train()
  for param in network.module.get_weights():
    param.requires_grad_(False)
  # update the architecture-weight
  network.module.set_cal_mode( 'joint' )
  for step, (_, _, arch_inputs, arch_targets) in enumerate(xloader):
    arch_targets = arch_targets.cuda(non_blocking=True)
    # measure data loading time
    data_time.update(time.time() - end)
    network.zero_grad()
    _, logits = network(arch_inputs)
    arch_loss = criterion(logits, arch_targets)
    arch_loss.backward()
    a_optimizer.step()
    # record
    arch_prec1, arch_prec5 = obtain_accuracy(logits.data, arch_targets.data, topk=(1, 5))
    arch_top1.update  (arch_prec1.item(), arch_inputs.size(0))
    arch_top5.update  (arch_prec5.item(), arch_inputs.size(0))
    arch_losses.update(arch_loss.item(),  arch_inputs.size(0))

    # measure elapsed time
    batch_time.update(time.time() - end)
    end = time.time()

    if step % print_freq == 0 or step + 1 == len(xloader):
      Sstr = '*SEARCH a* ' + time_string() + ' [{:}][{:03d}/{:03d}]'.format(epoch_str, step, len(xloader))
      Tstr = 'Time {batch_time.val:.2f} ({batch_time.avg:.2f}) Data {data_time.val:.2f} ({data_time.avg:.2f})'.format(batch_time=batch_time, data_time=data_time)
      Astr = 'Arch [Loss {loss.val:.3f} ({loss.avg:.3f})  Prec@1 {top1.val:.2f} ({top1.avg:.2f}) Prec@5 {top5.val:.2f} ({top5.avg:.2f})]'.format(loss=arch_losses, top1=arch_top1, top5=arch_top5)

      logger.log(Sstr + ' ' + Tstr + ' ' + Astr)
      #print (nn.functional.softmax(network.module.arch_parameters, dim=-1))
      #print (network.module.arch_parameters)
  return arch_losses.avg, arch_top1.avg, arch_top5.avg


def main(args):
  assert torch.cuda.is_available(), 'CUDA is not available.'
  torch.backends.cudnn.enabled   = True
  torch.backends.cudnn.benchmark = False
  torch.backends.cudnn.deterministic = True
  torch.set_num_threads( args.workers )
  prepare_seed(args.rand_seed)
  logger = prepare_logger(args)
  total_time = time.time()

  train_data, valid_data, xshape, class_num = get_datasets(args.dataset, args.data_path, args.cutout_length)
  config = load_config(args.config_path, {'class_num': class_num, 'xshape': xshape}, logger)
  search_loader, _, valid_loader = get_nas_search_loaders(train_data, valid_data, args.dataset, 'configs/nas-benchmark/', \
                                        config.batch_size if not hasattr(config, "test_batch_size") else (config.batch_size, config.test_batch_size), args.workers)
  logger.log('||||||| {:10s} ||||||| Search-Loader-Num={:}, Valid-Loader-Num={:}, batch size={:}'.format(args.dataset, len(search_loader), len(valid_loader), config.batch_size))
  logger.log('||||||| {:10s} ||||||| Config={:}'.format(args.dataset, config))

  search_space = get_search_spaces('cell', args.search_space_name)
  model_config = dict2config({'name': args.nas_name, 'C': args.channel, 'N': args.num_cells,
                                'max_nodes': args.max_nodes, 'num_classes': class_num,
                                'space'    : search_space,
                                'affine'   : False, 'track_running_stats': bool(args.track_running_stats)}, None)
  logger.log('search space : {:}'.format(search_space))
  logger.log('model-config : {:}'.format(model_config))

  search_model = get_cell_based_tiny_net(model_config)
  w_optimizer, w_scheduler, criterion = get_optim_scheduler(search_model.get_weights(), config)
  a_optimizer = torch.optim.Adam(search_model.get_alphas(), lr=args.arch_learning_rate, betas=(0.5, 0.999), weight_decay=args.arch_weight_decay)
  logger.log('w-optimizer : {:}'.format(w_optimizer))
  logger.log('a-optimizer : {:}'.format(a_optimizer))
  logger.log('w-scheduler : {:}'.format(w_scheduler))
  logger.log('criterion   : {:}'.format(criterion))
  flop, param  = get_model_infos(search_model, xshape)
  logger.log('{:}'.format(search_model))
  logger.log('FLOP = {:.2f} M, Params = {:.2f} MB'.format(flop, param))
  logger.log('search-space : {:}'.format(search_space))
  if args.search_space_name != "nas-bench-201" or args.num_cells != 5:
    api = None
  else:
    api = API(args.arch_nas_dataset)
    logger.log('{:} create API = {:} done'.format(time_string(), api))
  last_info, model_base_path, model_best_path = logger.path('info'), logger.path('model'), logger.path('best')

  network, criterion = torch.nn.DataParallel(search_model).cuda(), criterion.cuda()

  if last_info.exists() and not args.overwrite: # automatically resume from previous checkpoint
    logger.log("=> loading checkpoint of the last-info '{:}' start".format(last_info))
    last_info   = torch.load(last_info)
    start_epoch = last_info['epoch']
    checkpoint  = torch.load(last_info['last_checkpoint'])
    genotypes   = checkpoint['genotypes']
    arch_params = checkpoint['arch_params']
    search_losses = checkpoint['search_losses']
    valid_losses = checkpoint['valid_losses']
    search_arch_losses = checkpoint['search_arch_losses']
    search_model.load_state_dict( checkpoint['search_model'] )
    w_optimizer.load_state_dict ( checkpoint['w_optimizer'] )
    w_scheduler.load_state_dict ( checkpoint['w_scheduler'] )
    a_optimizer.load_state_dict ( checkpoint['a_optimizer'] )
    logger.log("=> loading checkpoint of the last-info '{:}' start with {:}-th epoch.".format(last_info, start_epoch))
  else:
    logger.log("=> do not find the last-info file : {:}".format(last_info))
    start_epoch, genotypes = 0, {-1: search_model.genotype(), "best": None}
    search_losses, search_arch_losses = {}, {}
    valid_losses, valid_acc1s, valid_acc5s = {}, {'best': -1}, {}
    arch_params = {}
  # start training
  (search_w_func, search_a_func), valid_func = get_search_methods(args.nas_name, 20)
  # specify search space
  n_sample = args.n_sample
  if sample_method:
      arch_path = os.environ['TORCH_HOME'] + "/all_archs-{}-test.pt".format(args.dataset)
      if os.path.isfile(arch_path):
          all_archs = torch.load(arch_path)["all_archs"]
      else:
          all_archs = list_arch(api, args.dataset, 'ori-test')
          save_checkpoint({'all_archs': all_archs}, arch_path, logger)
      # assert n_sample > 0, "[Picking search space] n_sample argument should be int. Now given {} with type {}".format(args.n_sample, type(args.n_sample))
      picked_archs = get_n_archs(all_archs, n_sample, sample_method)
      logger.log("[Picked search space] Dataset: {:}, Pick Method: {:}".format(args.dataset, sample_method))
      for arch_id in picked_archs:
          logger.log("Arch id [{:}], test accuracy [{:.2f}], arch_str [ {:} ], flops [{:}], params [{:}]]".format(arch_id, picked_archs[arch_id]['accuracy'], picked_archs[arch_id]['arch_str'], picked_archs[arch_id]['flop'], picked_archs[arch_id]['param']))
  else:
      picked_archs = None

  # search w training
  start_time, search_w_time, epoch_time, total_epoch = time.time(), AverageMeter(), AverageMeter(), config.epochs + config.warmup
  for epoch in range(start_epoch, total_epoch):
      w_scheduler.update(epoch, 0.0)
      need_time = 'Time Left: {:}'.format( convert_secs2time(epoch_time.val * (total_epoch-epoch), True) )
      epoch_str = '{:03d}-{:03d}'.format(epoch, total_epoch)
      if args.nas_name == "GDAS":
          search_model.set_tau( args.tau_max - (args.tau_max-args.tau_min) * epoch / (total_epoch-1) )
      logger.log('\n[Search the {:}-th epoch] {:}, LR={:}'.format(epoch_str, need_time, min(w_scheduler.get_lr())))
      search_w_loss, search_w_top1, search_w_top5 \
            = search_w_setn(search_loader, network, criterion, w_scheduler, w_optimizer, epoch_str, args.print_freq, logger, search_scope=picked_archs)
      search_w_time.update(time.time() - start_time)
      logger.log('[{:}] search [base] : loss={:.2f}, accuracy@1={:.2f}%, accuracy@5={:.2f}%, time-cost={:.1f} s'.format(epoch_str, search_w_loss, search_w_top1, search_w_top5, search_w_time.sum))
      search_losses[epoch] = search_w_loss
      # measure elapsed time
      eval_supernet()
      epoch_time.update(time.time() - start_time)
      start_time = time.time()
  # save checkpoint
  logger.log('<<<--->>> Supernet Train Complete.')
  save_path = save_checkpoint({'epoch' : epoch + 1,
              's_epoch': 0,
              'args'  : deepcopy(args),
              'search_model': search_model.state_dict(),
              'w_optimizer' : w_optimizer.state_dict(),
              'a_optimizer' : a_optimizer.state_dict(),
              'w_scheduler' : w_scheduler.state_dict(),
              'arch_params' : arch_params,
              'genotypes'   : deepcopy(genotypes),
              "search_losses" : deepcopy(search_losses),
              "search_arch_losses" : deepcopy(search_arch_losses),
              "valid_losses" : deepcopy(valid_losses),
              "valid_acc1s" : deepcopy(valid_acc1s),
              "valid_acc5s" : deepcopy(valid_acc5s),
              "search_scope" : picked_archs
              },
              model_base_path, logger)
  # search a training
  valid_time = AverageMeter()
  start_time, search_a_time, epoch_time, total_epoch = time.time(), AverageMeter(), AverageMeter(), 250 + config.warmup #config.epochs + config.warmup
  if not args.no_search:
      for s_epoch in range(start_epoch, total_epoch):
          need_time = 'Time Left: {:}'.format( convert_secs2time(epoch_time.val * (total_epoch-s_epoch), True) )
          epoch_str = '{:03d}-{:03d}'.format(s_epoch, total_epoch)
          if args.nas_name == "GDAS":
              search_model.set_tau( args.tau_max - (args.tau_max-args.tau_min) * s_epoch / (total_epoch-1) )
          logger.log('\n[Search the {:}-th epoch] {:}, LR={:}'.format(epoch_str, need_time, min(w_scheduler.get_lr())))
          search_a_loss, search_a_top1, search_a_top5 \
                = search_a_setn(search_loader, network, criterion, a_optimizer, epoch_str, args.print_freq, logger)
          search_a_time.update(time.time() - start_time)
          logger.log('[{:}] search [arch] : loss={:.2f}, accuracy@1={:.2f}%, accuracy@5={:.2f}%'.format(epoch_str, search_a_loss, search_a_top1, search_a_top5))
          # validation
          valid_start_time = time.time()
          if args.nas_name == "SETN":
              genotype, _ = get_best_arch(valid_loader, network, args.select_num)
              network.module.set_cal_mode('dynamic', genotype)
          else:
              genotype = search_model.genotype()
          valid_a_loss , valid_a_top1 , valid_a_top5  = valid_func(valid_loader, network, criterion)
          valid_time.update(time.time() - valid_start_time)
          logger.log('[{:}] evaluate : loss={:.2f}, accuracy@1={:.2f}%, accuracy@5={:.2f}%, time-cost={:1f} s'.format(epoch_str, valid_a_loss, valid_a_top1, valid_a_top5, valid_time.sum))
          # check the best accuracy
          search_arch_losses[s_epoch] = search_a_loss
          valid_losses[s_epoch] = valid_a_loss
          valid_acc1s[s_epoch] = valid_a_top1
          valid_acc5s[s_epoch] = valid_a_top5
          genotypes[s_epoch] = genotype
          with torch.no_grad():
              arch_param = nn.functional.softmax(search_model.arch_parameters, dim=-1).cpu().numpy()
          arch_params[s_epoch] = arch_param
          logger.log('<<<--->>> The {:}-th epoch : {:}'.format(epoch_str, genotypes[s_epoch]))
          if valid_a_top1 > valid_acc1s['best']:
              valid_acc1s['best'] = valid_a_top1
              genotypes['best']   = genotypes[s_epoch]
              arch_params['best'] = arch_param
              find_best = True
          else: find_best = False
          # save checkpoint
          save_path = save_checkpoint({'epoch' : epoch + 1,
                      's_epoch' : s_epoch,
                      'args'  : deepcopy(args),
                      'search_model': search_model.state_dict(),
                      'w_optimizer' : w_optimizer.state_dict(),
                      'a_optimizer' : a_optimizer.state_dict(),
                      'w_scheduler' : w_scheduler.state_dict(),
                      'arch_params' : arch_params,
                      'genotypes'   : deepcopy(genotypes),
                      "search_losses" : deepcopy(search_losses),
                      "search_arch_losses" : deepcopy(search_arch_losses),
                      "valid_losses" : deepcopy(valid_losses),
                      "valid_acc1s" : deepcopy(valid_acc1s),
                      "valid_acc5s" : deepcopy(valid_acc5s),
                      "search_scope" : picked_archs
                      },
                      model_base_path, logger)
          last_info = save_checkpoint({
                  'epoch': epoch + 1,
                  'args' : deepcopy(args),
                  'last_checkpoint': save_path,
                  }, logger.path('info'), logger)
          if find_best:
              logger.log('<<<--->>> The {:}-th epoch : find the highest validation accuracy : {:.2f}%.'.format(epoch_str, valid_a_top1))
              copy_checkpoint(model_base_path, model_best_path, logger)
          logger.log('arch-parameters :\n{:}'.format( arch_param ))
          if api is not None: logger.log('{:}'.format(api.query_by_arch( genotype )))
          # measure elapsed time
          epoch_time.update(time.time() - start_time)
          start_time = time.time()
  else:
      total_epoch = 0

  logger.log('\n' + '-'*100)
  check the performance from the architecture dataset
  logger.log('{:} : run {:} epochs, cost w-{:.1f} + a-{:.1f} s, last-geno is {:}.'.format(args.nas_name, total_epoch, search_w_time.sum, search_a_time.sum, genotypes[total_epoch-1]))
  if api is not None: logger.log('{:}'.format( api.query_by_arch(genotypes[total_epoch-1]) ))
  logger.log('The best-geno is {:} with Valid Acc {:}.'.format(genotypes['best'], valid_acc1s['best']))
  if api is not None: logger.log('{:}'.format( api.query_by_arch(genotypes['best']) ))
  logger.log("[Time cost] total: {:}, search w: {:}, search a: {:}, valid: {:}".format(convert_secs2time(time.time() - total_time, True), convert_secs2time(search_w_time.sum, True), convert_secs2time(search_a_time.sum, True), convert_secs2time(valid_time.sum, True)))
  logger.close()



if __name__ == '__main__':
  parser = argparse.ArgumentParser("Possibility check Experiment")
  parser.add_argument('--exp_name',           type=str,   default="",     help='Experiment name')
  parser.add_argument('--overwrite',          type=bool,  default=False,  help='Overwrite the existing results')
  parser.add_argument("--nas_name",           type=str,   default="SETN", help="NAS algorithm to use")
  parser.add_argument("--sample_method",      type=str)
  parser.add_argument('--n_sample',           type=int,   help='The number of top architectures to be scope. If negative, random archs are sampled.')
  parser.add_argument('--no_search',          type=bool,  default=False)
  # data
  parser.add_argument('--data_path',          type=str,   default=os.environ['TORCH_HOME'] + "/cifar.python", help='Path to dataset')
  parser.add_argument('--dataset',            type=str,   default='cifar10', choices=['cifar10', 'cifar100', 'ImageNet16-120'], help='Choose between Cifar10/100 and ImageNet-16.')
  parser.add_argument('--cutout_length',      type=int,   default=-1,      help='The cutout length, negative means not use.')
  # channels and number-of-cells
  parser.add_argument('--search_space_name',  type=str,   default="nas-bench-201", help='The search space name.')
  parser.add_argument('--max_nodes',          type=int,   default=4, help='The maximum number of nodes.')
  parser.add_argument('--channel',            type=int,   default=16, help='The number of channels.')
  parser.add_argument('--num_cells',          type=int,   default=5, help='The number of cells in one stage.')
  parser.add_argument('--track_running_stats',type=int,   default=0, choices=[0,1],help='Whether use track_running_stats or not in the BN layer.')
  parser.add_argument('--config_path',        type=str,   default="configs/research/possibility-E200.config", help='The path of the configuration.')
  parser.add_argument('--model_config',       type=str,   help='The path of the model configuration. When this arg is set, it will cover max_nodes / channels / num_cells.')
  # architecture leraning rate
  parser.add_argument('--arch_learning_rate', type=float, default=3e-4, help='learning rate for arch encoding')
  parser.add_argument('--arch_weight_decay',  type=float, default=1e-3, help='weight decay for arch encoding')
  # GDAS
  parser.add_argument('--tau_min',            type=float, default=0.1,  help='The minimum tau for Gumbel')
  parser.add_argument('--tau_max',            type=float, default=10,   help='The maximum tau for Gumbel')
  # SETN
  parser.add_argument("--select_num",         type=int,   default=100,  help="The number of architectures to be sampled for evaluation")
  # log
  parser.add_argument('--workers',            type=int,   default=8,    help='number of data loading workers')
  parser.add_argument('--save_dir',           type=str,   default="./output/possibility",     help='Folder to save checkpoints and log.')
  parser.add_argument('--arch_nas_dataset',   type=str,   default=os.environ['TORCH_HOME'] + "/NAS-Bench-201-v1_1-096897.pth", help='The path to load the architecture dataset (tiny-nas-benchmark).')
  parser.add_argument('--print_freq',         type=int,   default=100, help='print frequency (default: 100)')
  parser.add_argument('--rand_seed',          type=int,   default=-1, help='manual seed')
  args = parser.parse_args()
  if args.rand_seed is None or args.rand_seed < 0: args.rand_seed = random.randint(1, 100000)
  if args.exp_name != "":
      args.save_dir = "./output/possibility/{}/{}".format(args.dataset, args.exp_name)
  main(args)
  # top 10 archs
  # '|nor_conv_3x3~0|+|nor_conv_3x3~0|nor_conv_3x3~1|+|skip_connect~0|nor_conv_3x3~1|nor_conv_1x1~2|'
  # '|nor_conv_3x3~0|+|nor_conv_3x3~0|nor_conv_3x3~1|+|skip_connect~0|nor_conv_1x1~1|nor_conv_3x3~2|'
  # '|nor_conv_3x3~0|+|nor_conv_3x3~0|nor_conv_3x3~1|+|skip_connect~0|nor_conv_3x3~1|nor_conv_3x3~2|'
  # '|nor_conv_3x3~0|+|nor_conv_1x1~0|nor_conv_3x3~1|+|skip_connect~0|nor_conv_3x3~1|nor_conv_1x1~2|'
  # '|nor_conv_3x3~0|+|nor_conv_1x1~0|nor_conv_3x3~1|+|skip_connect~0|nor_conv_1x1~1|nor_conv_1x1~2|'
  # '|nor_conv_1x1~0|+|nor_conv_3x3~0|nor_conv_3x3~1|+|skip_connect~0|nor_conv_3x3~1|nor_conv_3x3~2|'
  # '|nor_conv_3x3~0|+|nor_conv_1x1~0|nor_conv_3x3~1|+|skip_connect~0|nor_conv_1x1~1|nor_conv_3x3~2|'
  # '|nor_conv_3x3~0|+|nor_conv_3x3~0|nor_conv_1x1~1|+|skip_connect~0|nor_conv_3x3~1|nor_conv_3x3~2|'
  # '|nor_conv_3x3~0|+|nor_conv_3x3~0|none~1|+|skip_connect~0|nor_conv_3x3~1|nor_conv_3x3~2|'
  # '|nor_conv_3x3~0|+|nor_conv_1x1~0|nor_conv_3x3~1|+|skip_connect~0|nor_conv_3x3~1|nor_conv_3x3~2|'
