import * as THREE from 'three';
import {OrbitControls} from './vendor/OrbitControls.js';

function meshEnvelope(positions,direction){
  const unique=new Map();for(let i=0;i<positions.count;i++){const p=[positions.getX(i),positions.getY(i)];unique.set(p.join(','),p);}
  const points=[...unique.values()].sort((a,b)=>a[0]-b[0]||a[1]-b[1]);
  if(points.length<3)return null;
  const cross=(a,b,c)=>(b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]);
  const chain=pts=>{const out=[];for(const p of pts){while(out.length>1&&cross(out.at(-2),out.at(-1),p)<=0)out.pop();out.push(p);}return out;};
  const hull=[...chain(points).slice(0,-1),...chain([...points].reverse()).slice(0,-1)];
  const directions=direction?[[direction[0],-direction[1]]]:hull.map((p,i)=>[hull[(i+1)%hull.length][0]-p[0],hull[(i+1)%hull.length][1]-p[1]]);
  let best=null,area=Infinity;
  for(const d of directions){const length=Math.hypot(...d);if(length<1e-9)continue;const u=d.map(x=>x/length),n=[-u[1],u[0]];
    const x=hull.map(p=>p[0]*u[0]+p[1]*u[1]),y=hull.map(p=>p[0]*n[0]+p[1]*n[1]);
    const sides=[Math.max(...x)-Math.min(...x),Math.max(...y)-Math.min(...y)];
    if(sides[0]*sides[1]<area){best=sides.sort((a,b)=>a-b);area=sides[0]*sides[1];}
  }
  return best;
}

// All assets are local. The drawing stays on the lower canvas while only this
// transparent scene's camera rotates. Each view has its own projection plane.
export class ModelLayer {
  constructor(container,onSelect,onFailure){
    this.container=container;this.onSelect=onSelect;this.onFailure=onFailure;this.visible=false;
    this.renderer=new THREE.WebGLRenderer({alpha:true,antialias:true,preserveDrawingBuffer:true});
    this.renderer.setPixelRatio(Math.min(devicePixelRatio||1,2));this.renderer.setClearColor(0x000000,0);
    this.renderer.outputColorSpace=THREE.SRGBColorSpace;this.renderer.domElement.id='model-canvas';
    this.renderer.domElement.setAttribute('aria-label','可旋转的当前结构面三维模型');container.append(this.renderer.domElement);
    this.scene=new THREE.Scene();this.camera=new THREE.OrthographicCamera(-1,1,1,-1,.01,100000);
    this.scene.add(new THREE.HemisphereLight(0xffffff,0x727c8c,2.5));
    const light=new THREE.DirectionalLight(0xffffff,3);light.position.set(200,300,1000);this.scene.add(light);
    this.controls=new OrbitControls(this.camera,this.renderer.domElement);this.controls.enableDamping=true;this.controls.dampingFactor=.12;
    this.controls.screenSpacePanning=true;this.controls.enablePan=true;this.controls.minZoom=.15;this.controls.maxZoom=25;
    this.controls.addEventListener('change',()=>this.draw());this.pickables=[];
    let down=null;
    this.renderer.domElement.addEventListener('pointerdown',e=>{down=[e.clientX,e.clientY];});
    this.renderer.domElement.addEventListener('pointerup',e=>{
      if(!down||Math.hypot(e.clientX-down[0],e.clientY-down[1])>4)return;
      const box=this.renderer.domElement.getBoundingClientRect(),p=new THREE.Vector2((e.clientX-box.left)/box.width*2-1,1-(e.clientY-box.top)/box.height*2);
      const ray=new THREE.Raycaster();ray.setFromCamera(p,this.camera);
      const found=ray.intersectObjects(this.pickables).find(h=>h.object.parent.visible&&h.object.visible);
      if(found)this.onSelect(found.object.userData.record);
    });
    this.renderer.domElement.addEventListener('webglcontextlost',e=>{e.preventDefault();this.visible=false;this.onFailure('三维绘图上下文丢失，请刷新页面重试。二维原图仍可使用。');});
    this.observer=new ResizeObserver(()=>this.resize());this.observer.observe(container.parentElement);
    const animate=()=>{requestAnimationFrame(animate);if(this.visible){this.controls.update();this.draw();}};animate();
  }
  clear(){
    if(this.root){this.root.traverse(o=>{o.geometry?.dispose();if(o.material){for(const m of [].concat(o.material))m.dispose();}});this.scene.remove(this.root);}
    this.root=new THREE.Group();this.scene.add(this.root);this.groups={};this.pickables=[];
    for(const kind of ['members','plates','holes']){const group=new THREE.Group();this.root.add(group);this.groups[kind]=group;}
  }
  solid(geometry,color,record,group){
    const material=new THREE.MeshStandardMaterial({color,metalness:.15,roughness:.62,transparent:true,opacity:this.opacity||.94,side:THREE.DoubleSide});
    const mesh=new THREE.Mesh(geometry,material);mesh.userData.record=record;group.add(mesh);this.pickables.push(mesh);
    const edges=new THREE.LineSegments(new THREE.EdgesGeometry(geometry,25),new THREE.LineBasicMaterial({color:color===0xe6b800?0x896800:0x3c5367,transparent:true,opacity:.6}));
    mesh.add(edges);return mesh;
  }
  setView(model,transform){
    this.model=model;this.transform=transform;this.clear();const b=model.bbox_pt,cx=(b[0]+b[2])/2,cy=(b[1]+b[3])/2;
    this.origin=[cx,cy];const ratio=model.scale.mm_per_pt;
    const xy=p=>[p[0]-cx,cy-p[1]];this.extent=Math.max(b[2]-b[0],b[3]-b[1],20);
    const maxLeg=Math.max(1,...model.members.map(m=>Math.max(...m.section.legs_mm)/(ratio||1)));
    this.layerGap=maxLeg*1.15;const lift=side=>side?.includes('后')?-this.layerGap:this.layerGap;
    const memberHeights=new Map(),plateHeights=new Map();
    if(ratio){
      for(const m of model.members){
        const [a1,a2]=m.section.legs_mm.map(x=>x/ratio),t=m.section.thickness_mm/ratio;
        const a=xy(m.axis_pt.a),b=xy(m.axis_pt.b),u=new THREE.Vector3(b[0]-a[0],b[1]-a[1],0),length=u.length();
        if(length<.01||t<=0||t>=Math.min(a1,a2))continue;u.normalize();const n=new THREE.Vector3(-u.y,u.x,0);
        const shape=new THREE.Shape();shape.moveTo(0,0);for(const p of [[a1,0],[a1,t],[t,t],[t,a2],[0,a2]])shape.lineTo(...p);shape.closePath();
        const geom=new THREE.ExtrudeGeometry(shape,{depth:length,steps:1,bevelEnabled:false,curveSegments:1});
        const mesh=this.solid(geom,0xe6b800,{kind:'members',...m},this.groups.members);
        mesh.matrixAutoUpdate=false;mesh.matrix.makeBasis(n,new THREE.Vector3(0,0,1),u);
        mesh.matrix.setPosition(a[0]-n.x*a1/2,a[1]-n.y*a1/2,lift(m.side));
        memberHeights.set(m.id,lift(m.side)+t+.1);
      }
      let detailVariant=0;
      for(const p of model.plates){
        if(!p.outline||!p.stock.thickness_mm)continue;
        const shape=new THREE.Shape();p.outline.polygon_pt.forEach((point,i)=>i?shape.lineTo(...xy(point)):shape.moveTo(...xy(point)));shape.closePath();
        for(const h of p.drill_holes){const [x,y]=xy(h.center_pt),hole=new THREE.Path();hole.absarc(x,y,h.diameter_mm/ratio/2,0,Math.PI*2,true);shape.holes.push(hole);}
        const geometry=new THREE.ExtrudeGeometry(shape,{depth:p.stock.thickness_mm/ratio,bevelEnabled:false,steps:1,curveSegments:24});
        const mesh=this.solid(geometry,0x7896b0,{kind:'plates',...p},this.groups.plates);
        mesh.position.z=p.side?.includes('后')?-this.layerGap-p.stock.thickness_mm/ratio:this.layerGap-p.stock.thickness_mm/ratio-.15;
        if(!model.members.length)mesh.position.z=(detailVariant++)*(p.stock.thickness_mm/ratio+4);
        plateHeights.set(p.id,mesh.position.z+p.stock.thickness_mm/ratio+.1);
      }
    }
    // Unknown diameters remain rings, never silently become drilled bores.
    for(const h of model.holes){
      const [x,y]=xy(h.center_pt),radius=h.diameter_mm&&ratio?h.diameter_mm/ratio/2:Math.min(...h.radii_pt);
      const geometry=new THREE.RingGeometry(Math.max(.08,radius-.32),radius+.32,28);
      const material=new THREE.MeshBasicMaterial({color:h.diameter_mm?0x8747ad:0xbb55a4,side:THREE.DoubleSide,transparent:true,opacity:.95,depthTest:false});
      const heights=[...h.plate_ids.map(id=>plateHeights.get(id)),...h.member_ids.map(id=>memberHeights.get(id))].filter(v=>v!==undefined);
      const mesh=new THREE.Mesh(geometry,material);mesh.position.set(x,y,heights.length?Math.max(...heights):.1);mesh.userData.record={kind:'holes',...h};mesh.renderOrder=10;
      this.groups.holes.add(mesh);this.pickables.push(mesh);
    }
    this.stats={view_id:model.view_id,angle_meshes:this.groups.members.children.length,plate_meshes:this.groups.plates.children.length,
      hole_markers:this.groups.holes.children.length,drilled_plate_holes:model.plates.reduce((n,p)=>n+p.drill_holes.length,0),
      materials:'BOM angle L sections and plate thicknesses',source_sha256:model.source_sha256};
    this.resize();this.align(transform);this.draw();
  }
  measureGeometry(){
    // Read the actual mesh vertices, not cached recipe lengths. Camera rotation
    // and the illustrative front/rear layer gaps are excluded from dimensions.
    const ratio=this.model?.scale.mm_per_pt, result={view_id:this.model?.view_id,members:[],plates:[],holes:[]};
    if(!ratio)return result;
    this.root.updateMatrixWorld(true);
    for(const mesh of this.groups.members.children){
      const positions=mesh.geometry.getAttribute('position'),axis=new THREE.Vector3(0,0,1).transformDirection(mesh.matrixWorld);
      let lo=Infinity,hi=-Infinity,xlo=Infinity,xhi=-Infinity,ylo=Infinity,yhi=-Infinity;
      for(let i=0;i<positions.count;i++){
        const local=new THREE.Vector3().fromBufferAttribute(positions,i),p=local.clone().applyMatrix4(mesh.matrixWorld),t=p.dot(axis);
        lo=Math.min(lo,t);hi=Math.max(hi,t);xlo=Math.min(xlo,local.x);xhi=Math.max(xhi,local.x);ylo=Math.min(ylo,local.y);yhi=Math.max(yhi,local.y);
      }
      result.members.push({id:mesh.userData.record.id,length_mm:(hi-lo)*ratio,legs_mm:[(xhi-xlo)*ratio,(yhi-ylo)*ratio]});
    }
    for(const mesh of this.groups.plates.children){
      const p=mesh.geometry.getAttribute('position');let lo=Infinity,hi=-Infinity;
      for(let i=0;i<p.count;i++){lo=Math.min(lo,p.getZ(i));hi=Math.max(hi,p.getZ(i));}
      result.plates.push({id:mesh.userData.record.id,thickness_mm:(hi-lo)*ratio,
        envelope_mm:meshEnvelope(p,mesh.userData.record.outline.stock_envelope_direction)?.map(x=>x*ratio)});
    }
    for(const mesh of this.groups.holes.children){
      const p=new THREE.Vector3();mesh.getWorldPosition(p);
      result.holes.push({id:mesh.userData.record.id,center_projection_mm:[p.x*ratio,p.y*ratio,0]});
    }
    return result;
  }
  resize(){
    const box={width:this.container.parentElement.clientWidth,height:this.container.parentElement.clientHeight};if(!box.width||!box.height)return;
    this.renderer.setSize(box.width,box.height,false);
    if(this.transform){const k=this.transform.k;this.camera.left=-box.width/2/k;this.camera.right=box.width/2/k;this.camera.top=box.height/2/k;this.camera.bottom=-box.height/2/k;this.camera.updateProjectionMatrix();}
    this.draw();
  }
  align(transform=this.transform){
    if(!this.model)return;this.transform=transform;this.resize();const [cx,cy]=this.origin;
    const x=-(cx*transform.k+transform.ox-transform.w/2)/transform.k,y=(cy*transform.k+transform.oy-transform.h/2)/transform.k;
    this.camera.zoom=1;this.camera.position.set(x,y,this.extent*4);this.camera.up.set(0,1,0);this.controls.target.set(x,y,0);
    this.camera.near=.01;this.camera.far=this.extent*30;this.camera.updateProjectionMatrix();this.controls.update();this.draw();
  }
  oblique(){const t=this.controls.target;this.camera.position.set(t.x+this.extent*.9,t.y+this.extent*.6,this.extent*2);this.controls.update();this.draw();}
  setVisible(visible){this.visible=visible;this.container.hidden=!visible;this.controls.enabled=visible;this.draw();}
  setLayers(options){for(const [k,g] of Object.entries(this.groups||{}))g.visible=options[k];this.draw();}
  setOpacity(value){this.opacity=value;this.root?.traverse(o=>{if(o.isMesh&&o.material.isMeshStandardMaterial)o.material.opacity=value;});this.draw();}
  draw(){if(this.model&&this.visible)this.renderer.render(this.scene,this.camera);}
}
