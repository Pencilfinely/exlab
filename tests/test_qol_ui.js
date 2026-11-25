'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../expman/static/app.js'), 'utf8');
const counts = {refresh: 0, matrices: 0, timers: 0, jobs: 0, projects: 0, overview: 0};
const intervals = [], listeners = {};
const env = vm.createContext({document: {hidden: true, addEventListener: (name, fn) => listeners[name] = fn},
  setInterval: fn => intervals.push(fn), token: 'test', currentView: 'overview', matrixResultId: null,
  refresh: () => counts.refresh++, loadMatrices: () => counts.matrices++, updateTimers: () => counts.timers++,
  $: id => { if (id === 'nodes') throw new Error('Hidden compute view was rebuilt'); return {}; },
  state: {jobs: [], nodes: []}, renderJobs: () => counts.jobs++, renderProjects: () => counts.projects++,
  renderOverview: () => counts.overview++});
vm.runInContext(source.slice(source.indexOf('function refreshVisiblePage()')), env);
for (let i = 0; i < 100; i++) intervals.forEach(fn => fn());
assert.deepEqual(counts, {refresh: 0, matrices: 0, timers: 0, jobs: 0, projects: 0, overview: 0});
env.document.hidden = false; listeners.visibilitychange();
assert.equal(counts.refresh, 1);
env.currentView = 'matrices'; intervals[0]();
assert.equal(counts.refresh, 2); assert.equal(counts.matrices, 1);
vm.runInContext(source.slice(source.indexOf('function render(){'), source.indexOf('function renderJobs(){')), env);
env.currentView = 'overview';env.render();
assert.equal(counts.overview, 1);assert.equal(counts.jobs, 0);assert.equal(counts.projects, 0);
env.currentView = 'experiments';env.render();assert.equal(counts.jobs, 1);
env.currentView = 'projects';env.render();assert.equal(counts.projects, 1);
console.log('PASS visibility pause, immediate foreground refresh and visible-view-only rendering');
const notice={}, timers=new Map();
let timerId=0;
const feedback=vm.createContext({$:()=>notice,setTimeout(fn,delay){timers.set(++timerId,{fn,delay});return timerId;},clearTimeout(id){timers.delete(id);}});
vm.runInContext(source.slice(source.indexOf('let noticeTimer;'),source.indexOf('async function writeClipboardText')),feedback);
feedback.notify('已暂停');
const first=timerId;
feedback.notify('恢复接单请求已发送');
assert.equal(notice.hidden,false);
assert.equal(timers.has(first),false);
assert.equal(timers.get(timerId).delay,6000);
timers.get(timerId).fn();
assert.equal(notice.hidden,true);
assert.equal(notice.textContent,'');
