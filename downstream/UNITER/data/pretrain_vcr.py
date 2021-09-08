from .vcr import VcrDetectFeatTxtTokDataset
from .mlm import random_word, random_word_dc
import torch
from toolz.sandbox import unzip
from torch.nn.utils.rnn import pad_sequence
from .data import pad_tensors, get_gather_index
from .mrm import (
    _get_img_tgt_mask, _get_img_mask, _mask_img_feat,
    _get_feat_target, _get_targets)
import numpy as np

class VcrPretrainDataset(VcrDetectFeatTxtTokDataset):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def _get_input_ids(self, txt_dump, mask=False):
        # text input
        input_ids_q = txt_dump['input_ids']
        type_ids_q = [0]*len(input_ids_q)
        if mask:
            input_ids_q, txt_labels_q = random_word(
                input_ids_q, self.txt_db.v_range,
                self.txt_db.mask)
        else:
            txt_labels_q = input_ids_q

        answer_label = txt_dump['qa_target']
        assert answer_label >= 0, "answer_label < 0"

        input_ids_a = txt_dump['input_ids_as'][answer_label]
        type_ids_a = [2]*len(input_ids_a)
        if mask:
            input_ids_a, txt_labels_a = random_word(
                input_ids_a, self.txt_db.v_range,
                self.txt_db.mask)
        else:
            txt_labels_a = input_ids_a

        input_ids = input_ids_q + [self.txt_db.sep] + input_ids_a
        type_ids = type_ids_q + [0] + type_ids_a
        txt_labels = txt_labels_q + [-1] + txt_labels_a

        if self.task == "qar":
            rationale_label = txt_dump['qar_target']
            assert rationale_label >= 0, "rationale_label < 0"

            input_ids_r = txt_dump['input_ids_rs'][rationale_label]
            type_ids_r = [3]*len(input_ids_r)
            if mask:
                input_ids_r, txt_labels_r = random_word(
                    input_ids_r, self.txt_db.v_range,
                    self.txt_db.mask)
            else:
                txt_labels_r = input_ids_r

            input_ids += [self.txt_db.sep] + input_ids_r
            type_ids += [2] + type_ids_r
            txt_labels += [-1] + txt_labels_r
        if mask:
            return input_ids, type_ids, txt_labels
        else:
            return input_ids, type_ids

    def combine_txt_inputs(self, input_ids, txt_type_ids, txt_labels=None):
        input_ids = torch.tensor([self.txt_db.cls_]
                                 + input_ids
                                 + [self.txt_db.sep])
        txt_type_ids = torch.tensor(
            [txt_type_ids[0]] + txt_type_ids
            + [txt_type_ids[-1]])

        if txt_labels is not None:
            txt_labels = torch.tensor([-1] + txt_labels + [-1])
            return input_ids, txt_type_ids, txt_labels
        return input_ids, txt_type_ids


def vcr_pretrain_collate(
        input_ids, txt_type_ids, img_feats,
        img_pos_feats, attn_masks):

    # text batches
    txt_lens = [i.size(0) for i in input_ids]
    input_ids = pad_sequence(input_ids, batch_first=True, padding_value=0)
    txt_type_ids = pad_sequence(txt_type_ids, batch_first=True,
                                padding_value=0)
    position_ids = torch.arange(0, input_ids.size(1), dtype=torch.long
                                ).unsqueeze(0)

    # image batches
    num_bbs = [f.size(0) for f in img_feats]
    img_feat = pad_tensors(img_feats, num_bbs)
    img_pos_feat = pad_tensors(img_pos_feats, num_bbs)

    attn_masks = pad_sequence(attn_masks, batch_first=True, padding_value=0)

    bs, max_tl = input_ids.size()
    out_size = attn_masks.size(1)
    gather_index = get_gather_index(txt_lens, num_bbs, bs, max_tl, out_size)

    batch = {'input_ids': input_ids,
             'txt_type_ids': txt_type_ids,
             'position_ids': position_ids,
             'img_feat': img_feat,
             'img_pos_feat': img_pos_feat,
             'attn_masks': attn_masks,
             'gather_index': gather_index}
    return batch


class MlmDatasetForVCR(VcrPretrainDataset):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def create_mlm_io(self, example):
        (input_ids, txt_type_ids,
         txt_labels) = self._get_input_ids(example, mask=True)
        return self.combine_txt_inputs(
            input_ids, txt_type_ids, txt_labels)
    
    ### use 'do-calculus' in UNITER pretrain : prepare method to extract label
    def _get_img_feat_for_db(self, img_db, fname):
        img_dump = img_db.get_dump(fname)
        img_feat = torch.tensor(img_dump['features'])
        bb = torch.tensor(img_dump['norm_bb'])
        img_bb = torch.cat([bb, bb[:, 4:5]*bb[:, 5:]], dim=-1)
        img_soft_label = torch.tensor(img_dump['soft_labels'])
        return img_feat, img_bb, img_soft_label

    def _get_img_feat(self, fname_gt, fname):
        if self.img_db and self.img_db_gt:
            (img_feat_gt, img_bb_gt,
             img_soft_label_gt) = self._get_img_feat_for_db(
                 self.img_db_gt, fname_gt)

            (img_feat, img_bb,
             img_soft_label) = self._get_img_feat_for_db(
                 self.img_db, fname)

            img_feat = torch.cat([img_feat_gt, img_feat], dim=0)
            img_bb = torch.cat([img_bb_gt, img_bb], dim=0)
            img_soft_label = torch.cat(
                [img_soft_label_gt, img_soft_label], dim=0)
        elif self.img_db:
            (img_feat, img_bb,
             img_soft_label) = self._get_img_feat_for_db(
                 self.img_db, fname)
        else:
            (img_feat, img_bb,
             img_soft_label) = self._get_img_feat_for_db(
                 self.img_db_gt, fname_gt)
        num_bb = img_feat.size(0)
        return img_feat, img_bb, img_soft_label, num_bb

    ###

    def __getitem__(self, i):
        example = super().__getitem__(i)
        img_feat, img_pos_feat, img_soft_labels, num_bb = self._get_img_feat(
            example['img_fname'][0], example['img_fname'][1])

        # txt inputs, create mlm io
        input_ids, txt_type_ids, txt_labels = self.create_mlm_io(example)

        attn_masks = torch.ones(
                len(input_ids) + num_bb,
                dtype=torch.long)

        return (input_ids, txt_type_ids, img_feat,
                img_soft_labels, img_pos_feat, attn_masks, txt_labels)


def mlm_collate_for_vcr(inputs):
    (input_ids, txt_type_ids, img_feats,
     img_soft_labels, img_pos_feats, attn_masks,
     txt_labels) = map(list, unzip(inputs))
    
    ### use 'do-calculus' in UNITER pretrain : prepare method to extract label
    num_bbs = [f.size(0) for f in img_feats]
    # img_soft_labels = pad_tensors(img_soft_labels, num_bbs)
    txt_lens = [i.size(0) for i in input_ids]
    ### 

    batch = vcr_pretrain_collate(
        input_ids, txt_type_ids, img_feats,
        img_pos_feats, attn_masks)
    txt_labels = pad_sequence(txt_labels, batch_first=True, padding_value=-1)

    batch['txt_labels'] = txt_labels
    ### use 'do-calculus' in UNITER pretrain : prepare method to extract label
    batch['img_soft_labels'] = img_soft_labels
    batch['txt_lens'] = txt_lens
    batch['num_bbs'] = num_bbs
    ###
    return batch


class MrfrDatasetForVCR(VcrPretrainDataset):
    def __init__(self, mask_prob, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mask_prob = mask_prob
    ### use 'do-calculus' in UNITER pretrain : prepare method to extract label
    def _get_img_feat_for_db(self, img_db, fname):
        img_dump = img_db.get_dump(fname)
        img_feat = torch.tensor(img_dump['features'])
        bb = torch.tensor(img_dump['norm_bb'])
        img_bb = torch.cat([bb, bb[:, 4:5]*bb[:, 5:]], dim=-1)
        img_soft_label = torch.tensor(img_dump['soft_labels'])
        return img_feat, img_bb, img_soft_label

    def _get_img_feat(self, fname_gt, fname):
        if self.img_db and self.img_db_gt:
            (img_feat_gt, img_bb_gt,
             img_soft_label_gt) = self._get_img_feat_for_db(
                 self.img_db_gt, fname_gt)

            (img_feat, img_bb,
             img_soft_label) = self._get_img_feat_for_db(
                 self.img_db, fname)

            img_feat = torch.cat([img_feat_gt, img_feat], dim=0)
            img_bb = torch.cat([img_bb_gt, img_bb], dim=0)
            img_soft_label = torch.cat(
                [img_soft_label_gt, img_soft_label], dim=0)
        elif self.img_db:
            (img_feat, img_bb,
             img_soft_label) = self._get_img_feat_for_db(
                 self.img_db, fname)
        else:
            (img_feat, img_bb,
             img_soft_label) = self._get_img_feat_for_db(
                 self.img_db_gt, fname_gt)
        num_bb = img_feat.size(0)
        return img_feat, img_bb, img_soft_label, num_bb

    ###

    def __getitem__(self, i):
        example = super().__getitem__(i)
        # text input
        input_ids, txt_type_ids = self._get_input_ids(example, mask=False)
        input_ids, txt_type_ids = self.combine_txt_inputs(
            input_ids, txt_type_ids)

        # image input features
        img_feat, img_pos_feat, img_soft_labels, num_bb = self._get_img_feat(
            example['img_fname'][0], example['img_fname'][1])
        img_mask = _get_img_mask(self.mask_prob, num_bb)
        img_mask_tgt = _get_img_tgt_mask(img_mask, len(input_ids))

        attn_masks = torch.ones(len(input_ids) + num_bb, dtype=torch.long)

        ### load vc feature to UNITER pretrain (mrfr)
        try:
            vc_name = '.'.join('_'.join(example['img_fname'][1].split('_')[2:]).split('.')[:-1])+".jpg.npy"
            vc_feat = torch.Tensor(np.load('vc_feature_final/'+vc_name))
            vc_feat_gt = torch.Tensor(np.load('vc_feature_gt/'+vc_name))
            
            nbbox_gt = self.img_db_gt[example['img_fname'][0]][0].shape[0]
            nbbox = self.img_db[example['img_fname'][1]][0].shape[0]

            '''
            if img_feat.shape[0]!=vc_feat.shape[0]+vc_feat_gt.shape[0]:
                print('\n*****************************')
                print(nbbox, nbbox_gt)
                print('\n*****************************')
                print(vc_feat.shape, vc_feat_gt.shape)
            '''
            if nbbox_gt > vc_feat_gt.shape[0]:
                vc_feat_gt = torch.cat((vc_feat_gt, torch.Tensor(nbbox_gt-vc_feat_gt.shape[0], vc_feat_gt.shape[1])), dim=0)
            if nbbox_gt < vc_feat_gt.shape[0]:
                vc_feat_gt.data = vc_feat_gt.data[:nbbox_gt, :]
            if nbbox > vc_feat.shape[0]:
                vc_feat = torch.cat((vc_feat, torch.Tensor(nbbox-vc_feat.shape[0], vc_feat.shape[1])), dim=0)
            if nbbox < vc_feat_gt.shape[0]:
                vc_feat.data = vc_feat.data[:nbbox, :]
            '''
            if nbbox_gt != vc_feat_gt.shape[0] or nbbox != vc_feat.shape[0]:
                vc_feat = torch.full((nbbox, 1024), -2)
                vc_feat_gt = torch.full((nbbox_gt, 1024), -2)
            '''
            vc_feat = torch.cat([vc_feat_gt, vc_feat], dim=0)
            if vc_feat.shape[0]!=img_feat.shape[0]:
                print('warning')
            #assert vc_feat.shape[0]==img_feat.shape[0]
            # print('error data')
        except:
          #import ipdb;ipdb.set_trace(context=10)
            #vc_name = '.'.join('_'.join(example['img_fname'][1].split('_')[2:]).split('.')[:-1])+".jpg.npy"
            #vc_feat = np.load('vc_feature_final/' + vc_name)
            vc_feat = img_feat
            #print('error data')

        #print(num_bb)
        #print(vc_feat.shape)
        #print(vc_feat_gt.shape)
        #print(vc_name)
        ###

        return (input_ids, txt_type_ids, img_feat, img_pos_feat,
                img_soft_labels, attn_masks, img_mask, img_mask_tgt, vc_feat)


def mrfr_collate_for_vcr(inputs):
   # import ipdb;ipdb.set_trace(context=10)
    (input_ids, txt_type_ids, img_feats, img_pos_feats, img_soft_labels,
     attn_masks, img_masks, img_mask_tgts, vc_feats) = map(list, unzip(inputs))
    
    ### use 'do-calculus' in UNITER pretrain : prepare method to extract label
    num_bbs = [f.size(0) for f in img_feats]
    # img_soft_label = pad_tensors(img_soft_labels, num_bbs)
    txt_lens = [i.size(0) for i in input_ids]
    ### 

    batch = vcr_mrfr_pretrain_collate(
        input_ids, txt_type_ids, img_feats,
        img_pos_feats, attn_masks, vc_feats)

    # mask features
    img_masks = pad_sequence(img_masks, batch_first=True, padding_value=0)
    feat_targets = _get_feat_target(batch['img_feat'], img_masks)

    ### pretrain by vc feat
    vc_feat_targets = _get_feat_target(batch['vc_feat'], img_masks)
    batch['vc_feat_targets'] = vc_feat_targets
    ### 
    
    img_mask_tgt = pad_sequence(
        img_mask_tgts, batch_first=True, padding_value=0)
    

    batch['img_feat'] = _mask_img_feat(batch['img_feat'], img_masks)
    batch['img_masks'] = img_masks
    batch['feat_targets'] = feat_targets
    batch['img_mask_tgt'] = img_mask_tgt
    ### use 'do-calculus' in UNITER pretrain : prepare method to extract label
    batch['img_soft_labels'] = img_soft_labels
    batch['txt_lens'] = txt_lens
    batch['num_bbs'] = num_bbs
    ###    
    return batch

def vcr_mrfr_pretrain_collate(
        input_ids, txt_type_ids, img_feats,
        img_pos_feats, attn_masks, vc_feats):

    # text batches
    txt_lens = [i.size(0) for i in input_ids]
    input_ids = pad_sequence(input_ids, batch_first=True, padding_value=0)
    txt_type_ids = pad_sequence(txt_type_ids, batch_first=True,
                                padding_value=0)
    position_ids = torch.arange(0, input_ids.size(1), dtype=torch.long
                                ).unsqueeze(0)

    # image batches
    num_bbs = [f.size(0) for f in img_feats]
    img_feat = pad_tensors(img_feats, num_bbs)
    img_pos_feat = pad_tensors(img_pos_feats, num_bbs)
    vc_feat = pad_tensors(vc_feats, num_bbs)

    attn_masks = pad_sequence(attn_masks, batch_first=True, padding_value=0)

    bs, max_tl = input_ids.size()
    out_size = attn_masks.size(1)
    gather_index = get_gather_index(txt_lens, num_bbs, bs, max_tl, out_size)

    batch = {'input_ids': input_ids,
             'txt_type_ids': txt_type_ids,
             'position_ids': position_ids,
             'img_feat': img_feat,
             'img_pos_feat': img_pos_feat,
             'attn_masks': attn_masks,
             'gather_index': gather_index,
             'vc_feat': vc_feat,
             'txt_lens': txt_lens}
    return batch


class MrcDatasetForVCR(VcrPretrainDataset):
    def __init__(self, mask_prob, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mask_prob = mask_prob

    def _get_img_feat_for_db(self, img_db, fname):
        img_dump = img_db.get_dump(fname)
        img_feat = torch.tensor(img_dump['features'])
        bb = torch.tensor(img_dump['norm_bb'])
        img_bb = torch.cat([bb, bb[:, 4:5]*bb[:, 5:]], dim=-1)
        img_soft_label = torch.tensor(img_dump['soft_labels'])
        return img_feat, img_bb, img_soft_label

    def _get_img_feat(self, fname_gt, fname):
        if self.img_db and self.img_db_gt:
            (img_feat_gt, img_bb_gt,
             img_soft_label_gt) = self._get_img_feat_for_db(
                 self.img_db_gt, fname_gt)

            (img_feat, img_bb,
             img_soft_label) = self._get_img_feat_for_db(
                 self.img_db, fname)

            img_feat = torch.cat([img_feat_gt, img_feat], dim=0)
            img_bb = torch.cat([img_bb_gt, img_bb], dim=0)
            img_soft_label = torch.cat(
                [img_soft_label_gt, img_soft_label], dim=0)
        elif self.img_db:
            (img_feat, img_bb,
             img_soft_label) = self._get_img_feat_for_db(
                 self.img_db, fname)
        else:
            (img_feat, img_bb,
             img_soft_label) = self._get_img_feat_for_db(
                 self.img_db_gt, fname_gt)
        num_bb = img_feat.size(0)
        return img_feat, img_bb, img_soft_label, num_bb

    def __getitem__(self, i):
        example = super().__getitem__(i)

        # text input
        input_ids, txt_type_ids = self._get_input_ids(example, mask=False)
        input_ids, txt_type_ids = self.combine_txt_inputs(
            input_ids, txt_type_ids)

        # image input features
        img_feat, img_pos_feat, img_soft_labels, num_bb = self._get_img_feat(
            example['img_fname'][0], example['img_fname'][1])
        img_mask = _get_img_mask(self.mask_prob, num_bb)
        img_mask_tgt = _get_img_tgt_mask(img_mask, len(input_ids))
        ### make label for unmasked object token (for 1_2)
        img_unmask = 1 - img_mask
        img_unmask_tgt = _get_img_tgt_mask(img_unmask, len(input_ids))
        ###

        attn_masks = torch.ones(len(input_ids) + num_bb, dtype=torch.long)

        return (input_ids, txt_type_ids, img_feat, img_pos_feat,
                img_soft_labels, attn_masks, img_mask, img_mask_tgt, img_unmask_tgt) ### make label for unmasked object token (for 1_2)


def mrc_collate_for_vcr(inputs):
    (input_ids, txt_type_ids, img_feats, img_pos_feats, img_soft_labels,
     attn_masks, img_masks, img_mask_tgts, img_unmask_tgts) = map(list, unzip(inputs))
    num_bbs = [f.size(0) for f in img_feats]

    ### use 'do-calculus' in UNITER pretrain : prepare method to extract label
    # img_soft_label = pad_tensors(img_soft_labels, num_bbs)
    txt_lens = [i.size(0) for i in input_ids]
    ### 

    batch = vcr_pretrain_collate(
        input_ids, txt_type_ids, img_feats,
        img_pos_feats, attn_masks)

    img_unmasks = [1-img_mask for img_mask in img_masks]        

    # mask features
    img_soft_label = pad_tensors(img_soft_labels, num_bbs)
    img_masks = pad_sequence(img_masks, batch_first=True, padding_value=0)
    img_unmasks = pad_sequence(img_unmasks, batch_first=True, padding_value=0)
    label_targets = _get_targets(img_masks, img_soft_label)
    label_targets_unmasked = _get_targets(img_unmasks, img_soft_label)
    img_mask_tgt = pad_sequence(
        img_mask_tgts, batch_first=True, padding_value=0)
    img_unmask_tgt = pad_sequence(
        img_unmask_tgts, batch_first=True, padding_value=0)
    batch['img_feat'] = _mask_img_feat(batch['img_feat'], img_masks)
    batch['img_masks'] = img_masks
    batch['label_targets'] = label_targets
    batch['img_mask_tgt'] = img_mask_tgt
    ### make label for unmasked object token (for 1_2)
    batch['label_targets_unmasked'] = label_targets_unmasked
    batch['img_unmask_tgt'] = img_unmask_tgt
    ### 
    ### use 'do-calculus' in UNITER pretrain : prepare method to extract label
    batch['img_soft_labels'] = img_soft_labels
    batch['txt_lens'] = txt_lens
    batch['num_bbs'] = num_bbs
    ###
    return batch

class DcDatasetForVCR(VcrPretrainDataset):
    def __init__(self, mask_prob, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mask_prob = mask_prob

    def _get_img_feat_for_db(self, img_db, fname):
        img_dump = img_db.get_dump(fname)
        img_feat = torch.tensor(img_dump['features'])
        bb = torch.tensor(img_dump['norm_bb'])
        img_bb = torch.cat([bb, bb[:, 4:5]*bb[:, 5:]], dim=-1)
        img_soft_label = torch.tensor(img_dump['soft_labels'])
        return img_feat, img_bb, img_soft_label

    def _get_img_feat(self, fname_gt, fname):
        if self.img_db and self.img_db_gt:
            (img_feat_gt, img_bb_gt,
             img_soft_label_gt) = self._get_img_feat_for_db(
                 self.img_db_gt, fname_gt)

            (img_feat, img_bb,
             img_soft_label) = self._get_img_feat_for_db(
                 self.img_db, fname)

            img_feat = torch.cat([img_feat_gt, img_feat], dim=0)
            img_bb = torch.cat([img_bb_gt, img_bb], dim=0)
            img_soft_label = torch.cat(
                [img_soft_label_gt, img_soft_label], dim=0)
        elif self.img_db:
            (img_feat, img_bb,
             img_soft_label) = self._get_img_feat_for_db(
                 self.img_db, fname)
        else:
            (img_feat, img_bb,
             img_soft_label) = self._get_img_feat_for_db(
                 self.img_db_gt, fname_gt)
        num_bb = img_feat.size(0)
        return img_feat, img_bb, img_soft_label, num_bb

    def __getitem__(self, i):
        example = super().__getitem__(i)

        # text input
        input_ids, txt_type_ids = self._get_input_ids(example, mask=False)
        input_ids, txt_type_ids = self.combine_txt_inputs(
            input_ids, txt_type_ids)

        # image input features
        img_feat, img_pos_feat, img_soft_labels, num_bb = self._get_img_feat(
            example['img_fname'][0], example['img_fname'][1])
        img_mask = _get_img_mask(self.mask_prob, num_bb)
        img_mask_tgt = _get_img_tgt_mask(img_mask, len(input_ids))

        attn_masks = torch.ones(len(input_ids) + num_bb, dtype=torch.long)

        return (input_ids, txt_type_ids, img_feat, img_pos_feat,
                img_soft_labels, attn_masks, img_mask, img_mask_tgt)


def dc_collate_for_vcr(inputs):
    (input_ids, txt_type_ids, img_feats, img_pos_feats, img_soft_labels,
     attn_masks, img_masks, img_mask_tgts) = map(list, unzip(inputs))
    num_bbs = [f.size(0) for f in img_feats]

    ### use 'do-calculus' in UNITER pretrain : prepare method to extract label
    # img_soft_label = pad_tensors(img_soft_labels, num_bbs)
    txt_lens = [i.size(0) for i in input_ids]
    ### 

    batch = vcr_pretrain_collate(
        input_ids, txt_type_ids, img_feats,
        img_pos_feats, attn_masks)

    # mask features
    img_soft_label = pad_tensors(img_soft_labels, num_bbs)
    img_masks = pad_sequence(img_masks, batch_first=True, padding_value=0)
    label_targets = _get_targets(img_masks, img_soft_label)
    img_mask_tgt = pad_sequence(
        img_mask_tgts, batch_first=True, padding_value=0)
    batch['img_feat'] = _mask_img_feat(batch['img_feat'], img_masks)
    batch['img_masks'] = img_masks
    batch['label_targets'] = label_targets
    batch['img_mask_tgt'] = img_mask_tgt
    ### use 'do-calculus' in UNITER pretrain : prepare method to extract label
    batch['img_soft_labels'] = img_soft_labels
    batch['txt_lens'] = txt_lens
    batch['num_bbs'] = num_bbs
    ###
    return batch


class VcrPretrainDatasetDC(VcrPretrainDataset):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.noun = np.load("./conf_and_prior_1_mrc_from_devlbert/id2class1155.npy", allow_pickle=True).item()

    def _get_input_ids(self, txt_dump, mask=False):
        # text input
        input_ids_q = txt_dump['input_ids']
        type_ids_q = [0]*len(input_ids_q)
        if mask:
            input_ids_q, txt_labels_q, causal_labels_q = random_word_dc(
                input_ids_q, self.txt_db.v_range,
                self.txt_db.mask, self.noun)
        else:
            txt_labels_q = input_ids_q
            causal_labels_q = input_ids_q

        answer_label = txt_dump['qa_target']
        assert answer_label >= 0, "answer_label < 0"

        input_ids_a = txt_dump['input_ids_as'][answer_label]
        type_ids_a = [2]*len(input_ids_a)
        if mask:
            input_ids_a, txt_labels_a, causal_labels_a = random_word_dc(
                input_ids_a, self.txt_db.v_range,
                self.txt_db.mask, self.noun)
        else:
            txt_labels_a = input_ids_a
            causal_labels_a = input_ids_a

        input_ids = input_ids_q + [self.txt_db.sep] + input_ids_a
        type_ids = type_ids_q + [0] + type_ids_a
        txt_labels = txt_labels_q + [-1] + txt_labels_a
        causal_labels = causal_labels_q + [-1] + causal_labels_a # [SEP]는 명사 안에 안 들어가니까

        if self.task == "qar":
            rationale_label = txt_dump['qar_target']
            assert rationale_label >= 0, "rationale_label < 0"

            input_ids_r = txt_dump['input_ids_rs'][rationale_label]
            type_ids_r = [3]*len(input_ids_r)
            if mask:
                input_ids_r, txt_labels_r, causal_labels_r = random_word_dc(
                    input_ids_r, self.txt_db.v_range,
                    self.txt_db.mask, self.noun)
            else:
                txt_labels_r = input_ids_r
                causal_labels_r = input_ids_r

            input_ids += [self.txt_db.sep] + input_ids_r
            type_ids += [2] + type_ids_r
            txt_labels += [-1] + txt_labels_r
            causal_labels += [-1] + causal_labels_r
        if mask:
            return input_ids, type_ids, txt_labels, causal_labels
        else:
            return input_ids, type_ids

    def combine_txt_inputs(self, input_ids, txt_type_ids, txt_labels=None):
        input_ids = torch.tensor([self.txt_db.cls_]
                                 + input_ids
                                 + [self.txt_db.sep])
        txt_type_ids = torch.tensor(
            [txt_type_ids[0]] + txt_type_ids
            + [txt_type_ids[-1]])

        if txt_labels is not None:
            txt_labels = torch.tensor([-1] + txt_labels + [-1])
            return input_ids, txt_type_ids, txt_labels
        return input_ids, txt_type_ids


def vcr_pretrain_collate(
        input_ids, txt_type_ids, img_feats,
        img_pos_feats, attn_masks):

    # text batches
    txt_lens = [i.size(0) for i in input_ids]
    input_ids = pad_sequence(input_ids, batch_first=True, padding_value=0)
    txt_type_ids = pad_sequence(txt_type_ids, batch_first=True,
                                padding_value=0)
    position_ids = torch.arange(0, input_ids.size(1), dtype=torch.long
                                ).unsqueeze(0)

    # image batches
    num_bbs = [f.size(0) for f in img_feats]
    img_feat = pad_tensors(img_feats, num_bbs)
    img_pos_feat = pad_tensors(img_pos_feats, num_bbs)

    attn_masks = pad_sequence(attn_masks, batch_first=True, padding_value=0)

    bs, max_tl = input_ids.size()
    out_size = attn_masks.size(1)
    gather_index = get_gather_index(txt_lens, num_bbs, bs, max_tl, out_size)

    batch = {'input_ids': input_ids,
             'txt_type_ids': txt_type_ids,
             'position_ids': position_ids,
             'img_feat': img_feat,
             'img_pos_feat': img_pos_feat,
             'attn_masks': attn_masks,
             'gather_index': gather_index}
    return batch

class MlmDatasetForVCRDC(VcrDetectFeatTxtTokDataset):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.noun = np.load("./conf_and_prior_1_mrc_from_devlbert/id2class1155.npy", allow_pickle=True).item()

    def _get_input_ids(self, txt_dump, mask=False):
        # text input
        input_ids_q = txt_dump['input_ids']
        type_ids_q = [0]*len(input_ids_q)
        if mask:
            input_ids_q, txt_labels_q, causal_labels_q = random_word_dc(
                input_ids_q, self.txt_db.v_range,
                self.txt_db.mask, self.noun)
        else:
            txt_labels_q = input_ids_q
            causal_labels_q = input_ids_q

        answer_label = txt_dump['qa_target']
        assert answer_label >= 0, "answer_label < 0"

        input_ids_a = txt_dump['input_ids_as'][answer_label]
        type_ids_a = [2]*len(input_ids_a)
        if mask:
            input_ids_a, txt_labels_a, causal_labels_a = random_word_dc(
                input_ids_a, self.txt_db.v_range,
                self.txt_db.mask, self.noun)
        else:
            txt_labels_a = input_ids_a
            causal_labels_a = input_ids_a

        input_ids = input_ids_q + [self.txt_db.sep] + input_ids_a
        type_ids = type_ids_q + [0] + type_ids_a
        txt_labels = txt_labels_q + [-1] + txt_labels_a
        causal_labels = causal_labels_q + [-1] + causal_labels_a # [SEP]는 명사 안에 안 들어가니까

        if self.task == "qar":
            rationale_label = txt_dump['qar_target']
            assert rationale_label >= 0, "rationale_label < 0"

            input_ids_r = txt_dump['input_ids_rs'][rationale_label]
            type_ids_r = [3]*len(input_ids_r)
            if mask:
                input_ids_r, txt_labels_r, causal_labels_r = random_word_dc(
                    input_ids_r, self.txt_db.v_range,
                    self.txt_db.mask, self.noun)
            else:
                txt_labels_r = input_ids_r
                causal_labels_r = input_ids_r

            input_ids += [self.txt_db.sep] + input_ids_r
            type_ids += [2] + type_ids_r
            txt_labels += [-1] + txt_labels_r
            causal_labels += [-1] + causal_labels_r
        if mask:
            return input_ids, type_ids, txt_labels, causal_labels
        else:
            return input_ids, type_ids

    def combine_txt_inputs(self, input_ids, txt_type_ids, txt_labels=None, causal_labels=None):
        input_ids = torch.tensor([self.txt_db.cls_]
                                 + input_ids
                                 + [self.txt_db.sep])
        txt_type_ids = torch.tensor(
            [txt_type_ids[0]] + txt_type_ids
            + [txt_type_ids[-1]])

        if txt_labels is not None:
            txt_labels = torch.tensor([-1] + txt_labels + [-1])
        if causal_labels is not None:
            causal_labels = torch.tensor([-1] + causal_labels + [-1])
            return input_ids, txt_type_ids, txt_labels, causal_labels

        return input_ids, txt_type_ids

    def create_mlm_io(self, example):
        (input_ids, txt_type_ids,
         txt_labels, causal_labels) = self._get_input_ids(example, mask=True)
        return self.combine_txt_inputs(
            input_ids, txt_type_ids, txt_labels, causal_labels)

    def _get_img_feat_for_db(self, img_db, fname):
        img_dump = img_db.get_dump(fname)
        img_feat = torch.tensor(img_dump['features'])
        bb = torch.tensor(img_dump['norm_bb'])
        img_bb = torch.cat([bb, bb[:, 4:5]*bb[:, 5:]], dim=-1)
        img_soft_label = torch.tensor(img_dump['soft_labels'])
        return img_feat, img_bb, img_soft_label

    def _get_img_feat(self, fname_gt, fname):
        if self.img_db and self.img_db_gt:
            (img_feat_gt, img_bb_gt,
             img_soft_label_gt) = self._get_img_feat_for_db(
                 self.img_db_gt, fname_gt)

            (img_feat, img_bb,
             img_soft_label) = self._get_img_feat_for_db(
                 self.img_db, fname)

            img_feat = torch.cat([img_feat_gt, img_feat], dim=0)
            img_bb = torch.cat([img_bb_gt, img_bb], dim=0)
            img_soft_label = torch.cat(
                [img_soft_label_gt, img_soft_label], dim=0)
        elif self.img_db:
            (img_feat, img_bb,
             img_soft_label) = self._get_img_feat_for_db(
                 self.img_db, fname)
        else:
            (img_feat, img_bb,
             img_soft_label) = self._get_img_feat_for_db(
                 self.img_db_gt, fname_gt)
        num_bb = img_feat.size(0)
        return img_feat, img_bb, img_soft_label, num_bb

    def __getitem__(self, i):
        example = super().__getitem__(i)
        img_feat, img_pos_feat, img_soft_labels, num_bb = self._get_img_feat(
            example['img_fname'][0], example['img_fname'][1])

        # txt inputs, create mlm io
        input_ids, txt_type_ids, txt_labels, causal_labels = self.create_mlm_io(example)

        attn_masks = torch.ones(
                len(input_ids) + num_bb,
                dtype=torch.long)

        return (input_ids, txt_type_ids, img_feat,
                img_soft_labels, img_pos_feat, attn_masks, txt_labels, causal_labels)


def mlm_collate_for_vcr_dc(inputs):
    (input_ids, txt_type_ids, img_feats,
     img_soft_labels, img_pos_feats, attn_masks,
     txt_labels, causal_labels) = map(list, unzip(inputs))
    
    ### use 'do-calculus' in UNITER pretrain : prepare method to extract label
    num_bbs = [f.size(0) for f in img_feats]
    # img_soft_labels = pad_tensors(img_soft_labels, num_bbs)
    txt_lens = [i.size(0) for i in input_ids]
    ### 

    batch = vcr_pretrain_collate(
        input_ids, txt_type_ids, img_feats,
        img_pos_feats, attn_masks)
    txt_labels = pad_sequence(txt_labels, batch_first=True, padding_value=-1)
    causal_labels = pad_sequence(causal_labels, batch_first=True, padding_value=-1)

    batch['txt_labels'] = txt_labels
    batch['causal_labels'] = causal_labels
    ### use 'do-calculus' in UNITER pretrain : prepare method to extract label
    batch['img_soft_labels'] = img_soft_labels
    batch['txt_lens'] = txt_lens
    batch['num_bbs'] = num_bbs
    ###
    return batch

'''
class MlmDatasetForVCR(VcrPretrainDataset):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def create_mlm_io(self, example):
        (input_ids, txt_type_ids,
         txt_labels) = self._get_input_ids(example, mask=True)
        return self.combine_txt_inputs(
            input_ids, txt_type_ids, txt_labels)
    
    ### use 'do-calculus' in UNITER pretrain : prepare method to extract label
    def _get_img_feat_for_db(self, img_db, fname):
        img_dump = img_db.get_dump(fname)
        img_feat = torch.tensor(img_dump['features'])
        bb = torch.tensor(img_dump['norm_bb'])
        img_bb = torch.cat([bb, bb[:, 4:5]*bb[:, 5:]], dim=-1)
        img_soft_label = torch.tensor(img_dump['soft_labels'])
        return img_feat, img_bb, img_soft_label

    def _get_img_feat(self, fname_gt, fname):
        if self.img_db and self.img_db_gt:
            (img_feat_gt, img_bb_gt,
             img_soft_label_gt) = self._get_img_feat_for_db(
                 self.img_db_gt, fname_gt)

            (img_feat, img_bb,
             img_soft_label) = self._get_img_feat_for_db(
                 self.img_db, fname)

            img_feat = torch.cat([img_feat_gt, img_feat], dim=0)
            img_bb = torch.cat([img_bb_gt, img_bb], dim=0)
            img_soft_label = torch.cat(
                [img_soft_label_gt, img_soft_label], dim=0)
        elif self.img_db:
            (img_feat, img_bb,
             img_soft_label) = self._get_img_feat_for_db(
                 self.img_db, fname)
        else:
            (img_feat, img_bb,
             img_soft_label) = self._get_img_feat_for_db(
                 self.img_db_gt, fname_gt)
        num_bb = img_feat.size(0)
        return img_feat, img_bb, img_soft_label, num_bb

    ###

    def __getitem__(self, i):
        example = super().__getitem__(i)
        img_feat, img_pos_feat, img_soft_labels, num_bb = self._get_img_feat(
            example['img_fname'][0], example['img_fname'][1])

        # txt inputs, create mlm io
        input_ids, txt_type_ids, txt_labels = self.create_mlm_io(example)

        attn_masks = torch.ones(
                len(input_ids) + num_bb,
                dtype=torch.long)

        return (input_ids, txt_type_ids, img_feat,
                img_soft_labels, img_pos_feat, attn_masks, txt_labels)
            '''