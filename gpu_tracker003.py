import numpy as np
import os
import sys
import time
import argparse
import yaml, json
from PIL import Image

import matplotlib.pyplot as plt

import torch
import torch.utils.data as data
import torch.optim as optim

sys.path.insert(0, '.')
from modules.model import MDNet0, MDNet1, BCELoss, set_optimizer
from modules.sample_generator import SampleGenerator
from modules.utils import overlap_ratio
from data_prov import RegionExtractor
from bbreg import BBRegressor
from gen_config import gen_config

opts = yaml.safe_load(open('tracking/options.yaml','r'))


def forward_samples(model, image, samples, out_layer='conv3'):
    model.eval()
    
    extractor = RegionExtractor(image, samples, opts)
    for i, regions in enumerate(extractor):
        
        if opts['use_gpu']:
            regions = regions.cuda()
        with torch.no_grad():
            feat = model(regions, out_layer=out_layer)
        if i==0:
            feats = feat.detach().clone()
        else:
            feats = torch.cat((feats, feat.detach().clone()), 0)
    return feats


def train(model, criterion, optimizer, pos_feats, neg_feats, maxiter, in_layer='fc4'):
    model.train()

    batch_pos = opts['batch_pos']
    batch_neg = opts['batch_neg']
    batch_test = opts['batch_test']
    batch_neg_cand = max(opts['batch_neg_cand'], batch_neg)

    pos_idx = np.random.permutation(pos_feats.size(0))
    neg_idx = np.random.permutation(neg_feats.size(0))
    while(len(pos_idx) < batch_pos * maxiter):
        pos_idx = np.concatenate([pos_idx, np.random.permutation(pos_feats.size(0))])
    while(len(neg_idx) < batch_neg_cand * maxiter):
        neg_idx = np.concatenate([neg_idx, np.random.permutation(neg_feats.size(0))])
    pos_pointer = 0
    neg_pointer = 0

    for i in range(maxiter):

        # select pos idx
        pos_next = pos_pointer + batch_pos
        pos_cur_idx = pos_idx[pos_pointer:pos_next]
        pos_cur_idx = pos_feats.new(pos_cur_idx).long()
        pos_pointer = pos_next

        # select neg idx
        neg_next = neg_pointer + batch_neg_cand
        neg_cur_idx = neg_idx[neg_pointer:neg_next]
        neg_cur_idx = neg_feats.new(neg_cur_idx).long()
        neg_pointer = neg_next

        # create batch
        batch_pos_feats = pos_feats[pos_cur_idx]
        batch_neg_feats = neg_feats[neg_cur_idx]

        # hard negative mining
        if batch_neg_cand > batch_neg:
            model.eval()
            for start in range(0, batch_neg_cand, batch_test):
                end = min(start + batch_test, batch_neg_cand)
                with torch.no_grad():
                    score = model(batch_neg_feats[start:end], in_layer=in_layer)
                if start==0:
                    neg_cand_score = score.detach()[:, 1].clone()
                else:
                    neg_cand_score = torch.cat((neg_cand_score, score.detach()[:, 1].clone()), 0)

            _, top_idx = neg_cand_score.topk(batch_neg)
            batch_neg_feats = batch_neg_feats[top_idx]
            model.train()

        # forward
        pos_score = model(batch_pos_feats, in_layer=in_layer)
        neg_score = model(batch_neg_feats, in_layer=in_layer)

        # optimize
        loss = criterion(pos_score, neg_score)
        model.zero_grad()
        loss.backward()
        if 'grad_clip' in opts:
            torch.nn.utils.clip_grad_norm_(model.parameters(), opts['grad_clip'])
        optimizer.step()


def run_mdnet(img_list, init_bbox, gt=None, savefig_dir='', display=False, model_path='models/model001.pth'):

    # Init bbox
    target_bbox = np.array(init_bbox)
    result = np.zeros((len(img_list), 4))
    result_bb = np.zeros((len(img_list), 4))
    result[0] = target_bbox
    result_bb[0] = target_bbox

    if gt is not None:
        overlap = np.zeros(len(img_list))
        overlap[0] = 1

    # Init model
    opts['model_path'] = model_path
    
    print('********')
    print('model:', opts['model_path'])
    print('********')
    
    assert(model_path == 'models/model000.pth' or model_path == 'models/model001.pth')
    
    if model_path == 'models/model000.pth': model = MDNet0(opts['model_path'])
    else: model = MDNet1(opts['model_path'])
    
    if opts['use_gpu']:
        model = model.cuda()

    # Init criterion and optimizer 
    criterion = BCELoss()
    model.set_learnable_params(opts['ft_layers'])
    init_optimizer = set_optimizer(model, opts['lr_init'], opts['lr_mult'])
    update_optimizer = set_optimizer(model, opts['lr_update'], opts['lr_mult'])

    tic = time.time()
    # Load first image
    image = Image.open(img_list[0]).convert('RGB')

    # Draw pos/neg samples
    pos_examples = SampleGenerator('gaussian', image.size, opts['trans_pos'], opts['scale_pos'])(
                        target_bbox, opts['n_pos_init'], opts['overlap_pos_init'])

    neg_examples = np.concatenate([
                    SampleGenerator('uniform', image.size, opts['trans_neg_init'], opts['scale_neg_init'])(
                        target_bbox, int(opts['n_neg_init'] * 0.5), opts['overlap_neg_init']),
                    SampleGenerator('whole', image.size)(
                        target_bbox, int(opts['n_neg_init'] * 0.5), opts['overlap_neg_init'])])
    neg_examples = np.random.permutation(neg_examples)

    # Extract pos/neg features
    pos_feats = forward_samples(model, image, pos_examples)
    print(pos_feats)
    neg_feats = forward_samples(model, image, neg_examples)
    print(neg_feats)

    # Initial training
    train(model, criterion, init_optimizer, pos_feats, neg_feats, opts['maxiter_init'])
    del init_optimizer, neg_feats
    torch.cuda.empty_cache()

    # Train bbox regressor
    bbreg_examples = SampleGenerator('uniform', image.size, opts['trans_bbreg'], opts['scale_bbreg'], opts['aspect_bbreg'])(
                        target_bbox, opts['n_bbreg'], opts['overlap_bbreg'])
    bbreg_feats = forward_samples(model, image, bbreg_examples)
    bbreg = BBRegressor(image.size)
    bbreg.train(bbreg_feats, bbreg_examples, target_bbox)
    del bbreg_feats
    torch.cuda.empty_cache()

    # Init sample generators for update
    sample_generator = SampleGenerator('gaussian', image.size, opts['trans'], opts['scale'])
    pos_generator = SampleGenerator('gaussian', image.size, opts['trans_pos'], opts['scale_pos'])
    neg_generator = SampleGenerator('uniform', image.size, opts['trans_neg'], opts['scale_neg'])

    # Init pos/neg features for update
    neg_examples = neg_generator(target_bbox, opts['n_neg_update'], opts['overlap_neg_init'])
    neg_feats = forward_samples(model, image, neg_examples)
    pos_feats_all = [pos_feats]
    neg_feats_all = [neg_feats]

    spf_total = time.time() - tic

    # Display
    savefig = savefig_dir != ''
    if display or savefig:
        dpi = 80.0
        figsize = (image.size[0] / dpi, image.size[1] / dpi)

        fig = plt.figure(frameon=False, figsize=figsize, dpi=dpi)
        ax = plt.Axes(fig, [0., 0., 1., 1.])
        ax.set_axis_off()
        fig.add_axes(ax)
        im = ax.imshow(image, aspect='auto')

        if gt is not None:
            gt_rect = plt.Rectangle(tuple(gt[0, :2]), gt[0, 2], gt[0, 3],
                                    linewidth=3, edgecolor="#00ff00", zorder=1, fill=False)
            ax.add_patch(gt_rect)

        rect = plt.Rectangle(tuple(result_bb[0, :2]), result_bb[0, 2], result_bb[0, 3],
                             linewidth=3, edgecolor="#ff0000", zorder=1, fill=False)
        ax.add_patch(rect)

        if display:
            plt.pause(.01)
            plt.draw()
        if savefig:
            fig.savefig(os.path.join(savefig_dir, '0000.jpg'), dpi=dpi)

    # Main loop
    for i in range(1, len(img_list)):

        tic = time.time()
        # Load image
        image = Image.open(img_list[i]).convert('RGB')

        # Estimate target bbox
        samples = sample_generator(target_bbox, opts['n_samples'])      
        sample_scores = forward_samples(model, image, samples, out_layer='fc6')

        top_scores, top_idx = sample_scores[:, 1].topk(5)

        # for top 5 samples, maximize score using hill-climbing algorithm
        for j in range(5):
            sample_ = samples[top_idx[j]]
            last_top_score = None

            # hill-climbing search
            while True:
                sample_left_p = [sample_[0]+1, sample_[1], sample_[2]-1, sample_[3]]
                sample_left_n = [sample_[0]-1, sample_[1], sample_[2]+1, sample_[3]]
                sample_up_p = [sample_[0], sample_[1]+1, sample_[2], sample_[3]-1]
                sample_up_n = [sample_[0], sample_[1]-1, sample_[2], sample_[3]+1]
                sample_right_p = [sample_[0], sample_[1], sample_[2]+1, sample_[3]]
                sample_right_n = [sample_[0], sample_[1], sample_[2]-1, sample_[3]]
                sample_bottom_p = [sample_[0], sample_[1], sample_[2], sample_[3]+1]
                sample_bottom_n = [sample_[0], sample_[1], sample_[2], sample_[3]-1]

                all_samples = [sample_left_p, sample_left_n, sample_up_p, sample_up_n, sample_right_p, sample_right_n, sample_bottom_p, sample_bottom_n]

                hillClimbingSS = forward_samples(model, image, np.array(all_samples), out_layer='fc6')
                top_score, top_index = hillClimbingSS[:, 1].topk(1)
                top_score_float = top_score.cpu().numpy()[0]
                    
                # End of hill climbing: this is THE BEST!
                if last_top_score != None:
                    if top_score_float < last_top_score: break
                    
                sample_ = all_samples[top_index]
                samples[top_idx[j]] = all_samples[top_index]
                last_top_score = top_score_float

        # modify sample scores array
        sample_scores = forward_samples(model, image, samples, out_layer='fc6')
        top_scores, top_idx = sample_scores[:, 1].topk(5)

        sampleStore = []
        for j in range(len(samples)):
            temp = []
            for k in range(4): temp.append(samples[j][k])
            sampleStore.append(temp)

        # if mean score of bbox < 0, find everywhere
        target_score = top_scores.mean()
        
        if target_score < 0:
            # print('')
            # print('last bbox:')
            # print(result[i-1])
            last_left = result[i-1][0]
            last_top = result[i-1][1]
            
            # print('')
            # for j in range(len(samples)): print(j, samples[j], sample_scores[j])
            # print('')
            # print('sample top scores (before):')
            # print(top_scores)
            # print(top_idx)

            cnt = 0
            rl = [32, 16]

            for _ in range(len(rl)):
                everywhere_sample = []

                # find everywhere (near the last bbox)
                meanWidth = 0.0
                meanHeight = 0.0
                for j in range(len(samples)):
                    meanWidth += samples[j][2]
                    meanHeight += samples[j][3]
                meanWidth /= len(samples)
                meanHeight /= len(samples)

                width = image.size[0]
                height = image.size[1]

                for j in range(32):
                    for k in range(32):
                        jk = [last_left + (31-2*j)*meanWidth/rl[_], last_top + (31-2*k)*meanHeight/rl[_], meanWidth, meanHeight]
                        # print(j, k, jk)
                        everywhere_sample.append(jk)
                
                everywhere_scores = forward_samples(model, image, np.array(everywhere_sample), out_layer='fc6')
                everywhere_top_scores, everywhere_top_idx = everywhere_scores[:, 1].topk(5)

                # print('')
                # print('everywhere_sample:')
                # for j in range(len(everywhere_sample)): print(j, everywhere_sample[j], everywhere_scores[j])

                # print('')
                # print('everywhere top scores (before):')
                # print(everywhere_top_scores)
                # print(everywhere_top_idx)
                # for j in range(5): print(everywhere_sample[everywhere_top_idx[j]])
                
                # for top 5 samples in everywhere_sample, maximize score using hill-climbing algorithm
                for j in range(5):
                    # print('')
                    sample_ = everywhere_sample[everywhere_top_idx[j]]
                    last_top_score = None

                    # hill-climbing search
                    while True:
                        sample_left_p = [sample_[0]+1, sample_[1], sample_[2]-1, sample_[3]]
                        sample_left_n = [sample_[0]-1, sample_[1], sample_[2]+1, sample_[3]]
                        sample_up_p = [sample_[0], sample_[1]+1, sample_[2], sample_[3]-1]
                        sample_up_n = [sample_[0], sample_[1]-1, sample_[2], sample_[3]+1]
                        sample_right_p = [sample_[0], sample_[1], sample_[2]+1, sample_[3]]
                        sample_right_n = [sample_[0], sample_[1], sample_[2]-1, sample_[3]]
                        sample_bottom_p = [sample_[0], sample_[1], sample_[2], sample_[3]+1]
                        sample_bottom_n = [sample_[0], sample_[1], sample_[2], sample_[3]-1]

                        all_samples = [sample_left_p, sample_left_n, sample_up_p, sample_up_n, sample_right_p, sample_right_n, sample_bottom_p, sample_bottom_n]

                        hillClimbingSS = forward_samples(model, image, np.array(all_samples), out_layer='fc6')
                        top_score, top_index = hillClimbingSS[:, 1].topk(1)
                        top_score_float = top_score.cpu().numpy()[0]
                            
                        # End of hill climbing: this is THE BEST!
                        if last_top_score != None:
                            # print(last_top_score)
                            if top_score_float < last_top_score: break
                            
                        sample_ = all_samples[top_index]
                        everywhere_sample[everywhere_top_idx[j]] = all_samples[top_index]
                        last_top_score = top_score_float

                everywhere_scores = forward_samples(model, image, np.array(everywhere_sample), out_layer='fc6')
                everywhere_top_scores, everywhere_top_idx = everywhere_scores[:, 1].topk(5)

                # print('')
                # print('everywhere top scores (after):')
                # print(everywhere_top_scores)
                # print(everywhere_top_idx)
                # for j in range(5): print(everywhere_sample[everywhere_top_idx[j]])

                # merge 'samples' with everywhere samples
                everywhere_top5 = []
                for j in range(5): everywhere_top5.append(everywhere_sample[everywhere_top_idx[j]])
                samples = np.concatenate((samples, np.array(everywhere_top5)))

                sample_scores = forward_samples(model, image, samples, out_layer='fc6')
                top_scores, top_idx = sample_scores[:, 1].topk(5)

                if top_scores.mean() > 0:
                    # print('')
                    # for j in range(len(samples)): print(j, samples[j], sample_scores[j])
                    # print('')
                    # print('sample top scores (after):')
                    # print(top_scores)
                    # print(top_idx)
                    break
                cnt += 1

            # failure -> recover original samples
            if cnt == 2:
                # print('recovered')
                samples = np.array(sampleStore)
                sample_scores = forward_samples(model, image, samples, out_layer='fc6')
                top_scores, top_idx = sample_scores[:, 1].topk(5)

        # finally modify sample scores array
        sample_scores = forward_samples(model, image, samples, out_layer='fc6')
        top_scores, top_idx = sample_scores[:, 1].topk(5)
        
        top_idx = top_idx.cpu()
        target_score = top_scores.mean()
        target_bbox = samples[top_idx]
        if top_idx.shape[0] > 1:
            target_bbox = target_bbox.mean(axis=0)
        success = target_score > 0
        
        # Expand search area at failure
        if success:
            sample_generator.set_trans(opts['trans'])
        else:
            sample_generator.expand_trans(opts['trans_limit'])

        # Bbox regression
        if success:
            bbreg_samples = samples[top_idx]
            if top_idx.shape[0] == 1:
                bbreg_samples = bbreg_samples[None,:]
            bbreg_feats = forward_samples(model, image, bbreg_samples)
            bbreg_samples = bbreg.predict(bbreg_feats, bbreg_samples)
            bbreg_bbox = bbreg_samples.mean(axis=0)
        else:
            bbreg_bbox = target_bbox

        # Save result
        result[i] = target_bbox
        result_bb[i] = bbreg_bbox

        # Data collect
        if success:
            pos_examples = pos_generator(target_bbox, opts['n_pos_update'], opts['overlap_pos_update'])
            pos_feats = forward_samples(model, image, pos_examples)
            pos_feats_all.append(pos_feats)
            if len(pos_feats_all) > opts['n_frames_long']:
                del pos_feats_all[0]

            neg_examples = neg_generator(target_bbox, opts['n_neg_update'], opts['overlap_neg_update'])
            neg_feats = forward_samples(model, image, neg_examples)
            neg_feats_all.append(neg_feats)
            if len(neg_feats_all) > opts['n_frames_short']:
                del neg_feats_all[0]

        # Short term update
        if not success:
            nframes = min(opts['n_frames_short'], len(pos_feats_all))
            pos_data = torch.cat(pos_feats_all[-nframes:], 0)
            neg_data = torch.cat(neg_feats_all, 0)
            train(model, criterion, update_optimizer, pos_data, neg_data, opts['maxiter_update'])

        # Long term update
        elif i % opts['long_interval'] == 0:
            pos_data = torch.cat(pos_feats_all, 0)
            neg_data = torch.cat(neg_feats_all, 0)
            train(model, criterion, update_optimizer, pos_data, neg_data, opts['maxiter_update'])

        torch.cuda.empty_cache()
        spf = time.time() - tic
        spf_total += spf

        # Display
        if display or savefig:
            im.set_data(image)

            if gt is not None:
                gt_rect.set_xy(gt[i, :2])
                gt_rect.set_width(gt[i, 2])
                gt_rect.set_height(gt[i, 3])

            rect.set_xy(result_bb[i, :2])
            rect.set_width(result_bb[i, 2])
            rect.set_height(result_bb[i, 3])

            if display:
                plt.pause(.01)
                plt.draw()
            if savefig:
                fig.savefig(os.path.join(savefig_dir, ('M' + model_path[14] + 'T3_' + '{:04d}.jpg'.format(i))), dpi=dpi)

        if gt is None:
            print('Frame {:d}/{:d}, Score {:.3f}, Time {:.3f}'
                .format(i, len(img_list), target_score, spf))
        else:
            overlap[i] = overlap_ratio(gt[i], result_bb[i])[0]
            print('Frame {:d}/{:d}, Overlap {:.3f}, Score {:.3f}, Time {:.3f}'
                .format(i, len(img_list), overlap[i], target_score, spf))

    if gt is not None:
        print('meanIOU: {:.3f}'.format(overlap.mean()))
    fps = len(img_list) / spf_total
    plt.close('all')
    return result, result_bb, fps, overlap

def main(args, model_path):
    print('args:', args.seq, args.json, args.savefig, args.display)
    np.random.seed(0)
    torch.manual_seed(0)

    # Generate sequence config
    img_list, init_bbox, gt, savefig_dir, display, result_path = gen_config(args)

    # Run tracker
    result, result_bb, fps, overlap = run_mdnet(img_list, init_bbox, gt=gt, savefig_dir=savefig_dir, display=display, model_path=model_path)

    # Save result
    res = {}
    res['res'] = result_bb.round().tolist()
    res['type'] = 'rect'
    res['fps'] = fps
    json.dump(res, open(result_path, 'w'), indent=2)

    return overlap, 'tracker002'

if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument('-s', '--seq', default='', help='input seq')
    parser.add_argument('-j', '--json', default='', help='input json')
    parser.add_argument('-f', '--savefig', action='store_true')
    parser.add_argument('-d', '--display', action='store_true')
    parser.add_argument('-m', '--model', default='model.pth')

    args = parser.parse_args()
    assert args.seq != '' or args.json != ''
    model_path = 'models/model001.pth'
    main(args, model_path)
