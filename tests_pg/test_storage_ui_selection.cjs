// The functions are extracted verbatim from the shipped browser bundle.
// This test does not access live services or other agents' worktrees.
'use strict';
const {readFileSync}=require('node:fs');
const {resolve}=require('node:path');
const vm=require('node:vm');
const assert=require('node:assert/strict');

const src=readFileSync(resolve(__dirname,'../app.js'),'utf8');
const start=src.indexOf('function normalizedKitPart(part){');
const end=src.indexOf('function privateKitReset(){',start);
assert(start>0&&end>start,'required CAD selector functions are present');
const code=src.slice(start,end)+
  '\nthis.selectCad = savedKitTargetForPart;this.normalizeCad = normalizedKitPart;';
const context={};
vm.runInNewContext(code,context);
const choose=context.selectCad;
const kits=[
 {kit_id:'kit-a',part:'focuser_drawtube',latest_revision:1},
 {kit_id:'kit-b',part:'lower_square_tube',latest_revision:8}
];
assert.equal(choose('  FOCUSER_DRAWTUBE ',kits,''),'kit-a');
assert.equal(choose('focuser_drawtube',kits,'kit-b'),'kit-a');
assert.equal(choose('focuser_drawtube',kits,'kit-a'),'kit-a');
assert.equal(choose('lower_square_tube',kits,''),'kit-b');
assert.equal(choose('different_part',kits,'kit-a'),'');
assert.equal(choose('',kits,'kit-b'),'kit-b');
assert.equal(choose(null,kits,'deleted-kit'),'');
assert.equal(choose('any',[],''),'');
assert.equal(choose('Деталь-1',[{kit_id:'kit-c',part:'деталь-1'}],''),'kit-c');
assert.equal(choose('x',null,''),'');
console.log('PASS 10 CAD selection cases: existing part, cross-part, unknown, whitespace, Unicode, absent listing');
