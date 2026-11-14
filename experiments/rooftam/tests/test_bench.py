import copy
import json
from pathlib import Path
import sys
import types

import numpy as np
from PIL import Image
import pytest
import torch
from torch import nn

from experiments.rooftam import bench
from experiments.rooftam.data import dataset_digest, load_manifest, load_sample, make_fixture, restore_logits
from experiments.rooftam.metrics import score_masks, summarize
from experiments.rooftam.models import EdgeRoof, segmentation_loss
from experiments.rooftam.runtime import select_device
from experiments.rooftam.export_sam3 import box_overlap


@pytest.fixture
def manifest(tmp_path):
    return make_fixture(tmp_path/'data')


def rewrite(path, change):
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    change(rows)
    path.write_text(''.join(json.dumps(x)+'\n' for x in rows))


def test_fixture_requires_explicit_opt_in(manifest):
    with pytest.raises(ValueError, match='Synthetic'):
        load_manifest(manifest)
    rows = load_manifest(manifest, True)
    assert len(rows) == 12
    assert {r['split'] for r in rows} == {'train','val','test'}
    assert dataset_digest(rows) == dataset_digest(copy.deepcopy(rows))


@pytest.mark.parametrize('field,value', [
    ('id','../unsafe'), ('gsd_m',0), ('gsd_m',float('nan')), ('split','dev'),
    ('label_status','agent_draft'), ('training_allowed',False), ('training_allowed','true'),
    ('cloud_upload_allowed',False), ('training_eligible',False), ('license',''), ('source_ref',''),
    ('prompt_source','ground_truth_mask'), ('label_kind','hidden_trusses'),
    ('box_xyxy',[-1,2,4,7]), ('box_xyxy',[5,5,2,2]), ('image','../missing.png')])
def test_rejects_invalid_data(manifest,field,value):
    rewrite(manifest,lambda rows: rows[0].update({field:value}))
    with pytest.raises(ValueError):
        load_manifest(manifest,True)


def test_duplicate_id(manifest):
    rewrite(manifest,lambda r:r[1].update(id=r[0]['id']))
    with pytest.raises(ValueError, match='duplicate'):
        load_manifest(manifest,True)


def test_geographic_leak(manifest):
    rewrite(manifest,lambda r:r[8].update(geo_group=r[0]['geo_group']))
    with pytest.raises(ValueError,match='geographic group leaks'):
        load_manifest(manifest,True)


def test_duplicate_image_across_splits(manifest):
    rewrite(manifest,lambda r:r[8].update(image=r[0]['image'],mask=r[0]['mask'],box_xyxy=r[0]['box_xyxy']))
    with pytest.raises(ValueError,match='identical imagery'):
        load_manifest(manifest,True)


def test_bad_mask(manifest):
    Image.new('L',(96,64),127).save(manifest.parent/'synthetic_00_mask.png')
    with pytest.raises(ValueError,match='binary'):
        load_manifest(manifest,True)


def test_preserves_rectangular_geometry(manifest):
    rows=load_manifest(manifest,True)
    for row in rows[:2]:
        image,target,valid,box,geometry=load_sample(row,64)
        h,w,nh,nw=geometry
        assert image.shape==(3,64,64)
        assert valid.sum()==nh*nw
        expected=np.array(row['box_xyxy'])*[nw/w,nh/h,nw/w,nh/h]
        np.testing.assert_allclose(box.numpy(),expected,rtol=1e-6)
        restored=restore_logits(target[0]*20-10,geometry,64)>0
        truth=np.asarray(Image.open(row['mask_path']))>0
        assert restored.shape==(h,w)
        assert score_masks(restored,truth,.1)['iou']>.9


def test_metrics_identity_empty_and_shift():
    truth=np.zeros((40,40),bool);truth[10:25,10:25]=True
    assert score_masks(truth,truth,.1)['iou']==1
    assert score_masks(truth,truth,.1)['boundary_f1_0.25m']==1
    empty=np.zeros_like(truth)
    assert score_masks(empty,empty,.1)['iou']==1
    assert score_masks(empty,truth,.1)['boundary_f1_1m']==0
    shifted=np.roll(truth,1,axis=1)
    result=score_masks(shifted,truth,1)
    assert result['boundary_f1_0.25m']<result['boundary_f1_1m']==1
    assert result['area_error_m2']==0


def test_loss_has_finite_gradient_and_ignores_padding():
    logits=torch.zeros(1,1,16,16,requires_grad=True)
    target=torch.zeros_like(logits);target[:,:,3:8,3:8]=1
    valid=torch.ones_like(logits);valid[:,:,:,12:]=0
    segmentation_loss(logits,target,valid).backward()
    assert torch.isfinite(logits.grad).all()
    assert logits.grad.abs().sum()>0
    assert logits.grad[:,:,:,12:].abs().sum()==0


def test_tpu_has_no_silent_fallback(monkeypatch):
    import experiments.rooftam.runtime as runtime
    def absent(name):
        raise runtime.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(runtime.metadata,'version',absent)
    with pytest.raises(RuntimeError,match='torch_xla is missing'):
        select_device('tpu')


def test_independent_box_association():
    mask=np.zeros((20,20),bool);mask[4:10,3:12]=1
    assert box_overlap(mask,[3,4,12,10])==1
    assert box_overlap(mask,[15,15,19,19])==0


def test_tiny_end_to_end_and_saved_adapter(manifest,tmp_path):
    train=tmp_path/'train'; evaluation=tmp_path/'eval'
    shared=['--manifest',str(manifest),'--allow-synthetic','--model','tiny','--side','64']
    bench.main(['train',*shared,'--steps','3','--output',str(train)])
    run=json.loads((train/'run.json').read_text())
    assert run['status']=='passed' and run['weights_changed'] and run['synthetic']
    bench.main(['evaluate',*shared,'--adapter',str(train/'adapter.pt'),'--output',str(evaluation)])
    result=json.loads((evaluation/'evaluation.json').read_text())
    assert result['summary']['n']==2 and result['split']=='val'
    assert len(list((evaluation/'predictions').glob('*.png')))==2
    with pytest.raises(ValueError,match='Output must be empty'):
        bench.main(['evaluate',*shared,'--output',str(evaluation)])


def test_failure_artifact(manifest,tmp_path):
    out=tmp_path/'failed'
    with pytest.raises(ValueError):
        bench.main(['train','--model','tiny','--manifest',str(manifest),'--output',str(out)])
    assert json.loads((out/'failure.json').read_text())['status']=='failed'


def test_compare_refuses_different_splits(tmp_path):
    sample=dict(id='a',image_sha256='i',mask_sha256='m')
    report=dict(status='passed',dataset_sha256='d',split='val',synthetic=True,samples=[sample],summary={'iou':1})
    a=tmp_path/'a.json';b=tmp_path/'b.json'
    a.write_text(json.dumps(report));report['split']='test';b.write_text(json.dumps(report))
    with pytest.raises(ValueError,match='identical samples'):
        bench.main(['compare','--reports',str(a),str(b),'--output',str(tmp_path/'comp')])


def test_edge_adapter_gradient_contract(monkeypatch,tmp_path):
    # Test double proves our adapter does not detach decoder gradients; not an EdgeTAM/TPU execution.
    class Prompt(nn.Module):
        def forward(self,points,boxes,masks):
            return boxes[:,None,:],boxes[:,None,:]
        def get_dense_pe(self):
            return None
    class Decoder(nn.Conv2d):
        def __init__(self): super().__init__(8,1,1)
        def forward(self,**kw): return super().forward(kw['image_embeddings']),None,None,None
    class Fake(nn.Module):
        num_feature_levels=1
        directly_add_no_mem_embed=True
        def __init__(self):
            super().__init__()
            self.image_encoder=nn.Sequential(nn.Conv2d(3,8,32,32),nn.BatchNorm2d(8))
            self.sam_prompt_encoder=Prompt();self.sam_mask_decoder=Decoder()
            self.no_mem_embed=nn.Parameter(torch.zeros(1,1,8))
        def forward_image(self,x): return {'backbone_fpn':[self.image_encoder(x)]}
    module=types.ModuleType('sam2.build_sam');module.build_sam2=lambda *a,**k:Fake()
    monkeypatch.setitem(sys.modules,'sam2',types.ModuleType('sam2'))
    monkeypatch.setitem(sys.modules,'sam2.build_sam',module)
    checkpoint=tmp_path/'fake.pt';checkpoint.touch()
    model=EdgeRoof(str(checkpoint)).train()
    bn=model.model.image_encoder[1]
    before=bn.running_mean.clone()
    out=model(torch.rand(1,3,1024,1024),torch.tensor([[1.,1.,900.,900.]]))
    out.mean().backward()
    assert model.model.sam_mask_decoder.weight.grad is not None
    assert model.model.image_encoder[0].weight.grad is None
    assert torch.equal(before,bn.running_mean) and not bn.training


def test_notebook_is_valid_python():
    import nbformat
    path=Path(__file__).parents[1]/'kaggle'/'rooftam_tpu.ipynb'
    notebook=nbformat.read(path,as_version=4)
    nbformat.validate(notebook)
    for cell in notebook.cells:
        if cell.cell_type=='code':
            compile(cell.source,str(path),'exec')
