window.previewReady = false;
window.MathJax = {
  tex: {inlineMath: [['\\(', '\\)']], displayMath: [['\\[', '\\]']]},
  startup: {
    ready: () => {
      MathJax.startup.defaultReady();
      MathJax.startup.promise.then(() => document.fonts.ready).then(() => {
        window.previewReady = true;
      });
    }
  }
};
