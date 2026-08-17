// Custom bootstrap template; the two placeholder lines below are substituted
// at build time. Pins CanvasKit to the copy bundled in this build so the
// client runs fully self-contained -- the engine's default is to fetch it
// from gstatic.com at startup, which an off-grid host can't do.
{{flutter_js}}
{{flutter_build_config}}

_flutter.loader.load({
  config: { canvasKitBaseUrl: "canvaskit/" },
});
