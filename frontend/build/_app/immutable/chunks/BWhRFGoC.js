var c=e=>{throw TypeError(e)};var v=(e,s,t)=>s.has(e)||c("Cannot "+t);var d=(e,s,t)=>(v(e,s,"read from private field"),t?t.call(e):s.get(e)),m=(e,s,t)=>s.has(e)?c("Cannot add the same private member more than once"):s instanceof WeakSet?s.add(e):s.set(e,t);import"./CWj6FrbW.js";import"./BPBWoWSA.js";import{aj as p,bi as x,aV as y,am as N,b6 as b}from"./BoGpxLG1.js";import{d as u,a as h}from"./DotZi6i7.js";import{I as l,s as $}from"./BgofuWQ1.js";import{l as f,a as g}from"./CyiahROV.js";function V(e,s){const t=f(s,["children","$$slots","$$events","$$legacy"]);/**
 * @license lucide-svelte v0.460.1 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const r=[["path",{d:"M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"}],["path",{d:"M10.3 21a1.94 1.94 0 0 0 3.4 0"}]];l(e,g({name:"bell"},()=>t,{get iconNode(){return r},children:(i,_)=>{var o=u(),n=p(o);$(n,s,"default",{}),h(i,o)},$$slots:{default:!0}}))}function X(e,s){const t=f(s,["children","$$slots","$$events","$$legacy"]);/**
 * @license lucide-svelte v0.460.1 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const r=[["path",{d:"M18 6 6 18"}],["path",{d:"m6 6 12 12"}]];l(e,g({name:"x"},()=>t,{get iconNode(){return r},children:(i,_)=>{var o=u(),n=p(o);$(n,s,"default",{}),h(i,o)},$$slots:{default:!0}}))}let w=1;var a;class B{constructor(){m(this,a,x(y([])))}get items(){return N(d(this,a))}set items(s){b(d(this,a),s,!0)}push(s,t,r=4500){const i=w++;return this.items=[...this.items,{id:i,kind:s,message:t,ttl:r}],r>0&&setTimeout(()=>this.dismiss(i),r),i}success(s,t){return this.push("success",s,t)}error(s,t){return this.push("error",s,t??6500)}warn(s,t){return this.push("warn",s,t)}info(s,t){return this.push("info",s,t)}dismiss(s){this.items=this.items.filter(t=>t.id!==s)}}a=new WeakMap;const k=new B;export{V as B,X,k as t};
