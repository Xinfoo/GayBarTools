/* Local demonstration only. These counters observe native browser events. */
window.demoChapter = new URLSearchParams(location.search).get('chapterId') || '1';
window.recordDemo = function (section, values) {
  const all = JSON.parse(localStorage.getItem('gaybar-demo-metrics') || '{}');
  const chapter = all[window.demoChapter] || {};
  chapter[section] = {...chapter[section], ...values};
  all[window.demoChapter] = chapter;
  localStorage.setItem('gaybar-demo-metrics', JSON.stringify(all));
};
window.signalDemo = function (type, detail = {}) {
  window.top.postMessage({source: 'gaybar-demo', type, chapterId: window.demoChapter, ...detail}, location.origin);
};
