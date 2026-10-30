import test from 'node:test';
import assert from 'node:assert/strict';
import { memoryStatusLabel,canEnableCloud,tokenLabel } from './memorySettings.ts';
test('memory runtime is optional on older schemas',()=>assert.match(memoryStatusLabel({supported:false,enabled:false,paused:true}),/v6/));
test('cloud selection alone is not authorization',()=>{assert.equal(canEnableCloud('deepseek',false,false),false);assert.equal(canEnableCloud('deepseek',true,false),true);assert.equal(canEnableCloud('mock',false,false),true);});
test('paused jobs stay visible',()=>assert.match(memoryStatusLabel({supported:true,enabled:true,paused:true}),/保留/));
test('unknown usage cannot become fabricated real tokens',()=>{assert.equal(tokenLabel(), '未知');assert.match(tokenLabel({api_tokens:12,unknown_requests:1}),/1 次用量未知/);});
