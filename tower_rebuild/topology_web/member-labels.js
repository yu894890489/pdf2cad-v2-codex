import * as THREE from 'three';

const visibleInTree=object=>{for(let o=object;o;o=o.parent)if(!o.visible)return false;return true;};

// Physical-sized, two-sided plaques live in a separate scene group. They
// follow member faces without entering structural bounds, dimensions or exports.
export class MemberLabels{
  constructor(scene,onSelect){
    this.scene=scene;this.onSelect=onSelect;this.items=[];this.pickables=[];
    this.root=new THREE.Group();this.root.name='member-placards';scene.add(this.root);
    this.translation=new THREE.Matrix4();this.rotation=new THREE.Matrix4();this.scaling=new THREE.Matrix4();
  }
  clear(){
    this.root.traverse(o=>{o.geometry?.dispose();for(const m of o.material?[].concat(o.material):[]){m.map?.dispose();m.dispose();}});
    this.root.clear();this.items=[];this.pickables=[];
  }
  setItems(items){
    this.clear();
    for(const item of items){
      const height=item.labelHeight||100;
      const canvas=document.createElement('canvas'),measure=canvas.getContext('2d');
      measure.font='600 60px "Segoe UI","Microsoft YaHei",sans-serif';
      canvas.width=Math.ceil(Math.max(100,measure.measureText(item.text).width+28));canvas.height=80;
      const ctx=canvas.getContext('2d');ctx.font='600 60px "Segoe UI","Microsoft YaHei",sans-serif';
      ctx.fillStyle='#352908';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(item.text,canvas.width/2,43);
      const texture=new THREE.CanvasTexture(canvas);texture.colorSpace=THREE.SRGBColorSpace;
      const width=height*canvas.width/canvas.height,thickness=height*.025;
      const group=new THREE.Group();group.name='member-mark:'+item.key;group.matrixAutoUpdate=false;
      const body=new THREE.Mesh(new THREE.BoxGeometry(width,height,thickness),new THREE.MeshBasicMaterial({color:0xfff4cb,depthTest:true,depthWrite:true}));
      body.userData.memberLabelKey=item.key;group.add(body);this.pickables.push(body);
      const border=new THREE.LineSegments(new THREE.EdgesGeometry(body.geometry),new THREE.LineBasicMaterial({color:0x967321,depthTest:true}));group.add(border);
      // Separate front/back faces keep glyphs readable from either side.
      for(const back of [false,true]){
        const face=new THREE.Mesh(new THREE.PlaneGeometry(width,height),new THREE.MeshBasicMaterial({map:back?texture.clone():texture,transparent:true,alphaTest:.2,side:THREE.FrontSide,depthTest:true,depthWrite:true,polygonOffset:true,polygonOffsetFactor:-1,polygonOffsetUnits:-1}));
        face.position.z=(back?-1:1)*thickness*.55;if(back)face.rotation.y=Math.PI;group.add(face);
      }
      const u=(item.direction||new THREE.Vector3(1,0,0)).clone().setZ(0).normalize();
      if(u.lengthSq()<.5)u.set(1,0,0);
      // Keep type upright in the source face; orientation is not camera-driven.
      if(u.x<0||(Math.abs(u.x)<1e-8&&u.y<0))u.negate();
      const n=new THREE.Vector3(-u.y,u.x,0),clearance=(item.memberWidth||height*.5)/2+height*.8;
      const position=item.point.clone().addScaledVector(n,clearance);position.z+=height*.12;
      const stemEnd=position.clone().addScaledVector(n,-height/2);
      const stem=new THREE.Line(new THREE.BufferGeometry().setFromPoints([item.point,stemEnd]),new THREE.LineBasicMaterial({color:0x967321,depthTest:true,depthWrite:true}));
      stem.matrixAutoUpdate=false;this.root.add(stem,group);
      this.items.push({...item,group,body,stem,position,angle:Math.atan2(u.y,u.x),height,width});
    }
  }
  update(camera,{enabled=true,include=()=>true,selectedKey=null,size=1}={}){
    this.root.visible=enabled;if(!enabled)return;
    const updated=new Set();
    for(const item of this.items){
      const show=include(item)&&visibleInTree(item.object);item.group.visible=item.stem.visible=show;
      if(!show)continue;
      if(!updated.has(item.object)){item.object.updateWorldMatrix(true,false);updated.add(item.object);}
      this.translation.makeTranslation(...item.position.toArray());this.rotation.makeRotationZ(item.angle);this.scaling.makeScale(size,size,size);
      item.group.matrix.copy(item.object.matrixWorld).multiply(this.translation).multiply(this.rotation).multiply(this.scaling);
      item.group.matrixWorldNeedsUpdate=true;
      item.stem.matrix.copy(item.object.matrixWorld);item.stem.matrixWorldNeedsUpdate=true;
      item.body.material.color.set(item.key===selectedKey?0xffcb73:0xfff4cb);
    }
    this.root.updateMatrixWorld(true);
  }
  pick(ray,occluders=[]){
    if(!this.root.visible)return null;
    const hit=ray.intersectObjects([...this.pickables,...occluders],false).find(h=>visibleInTree(h.object));
    return hit?.object.userData.memberLabelKey?this.items.find(item=>item.key===hit.object.userData.memberLabelKey):null;
  }
}
