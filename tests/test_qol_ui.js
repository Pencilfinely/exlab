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
