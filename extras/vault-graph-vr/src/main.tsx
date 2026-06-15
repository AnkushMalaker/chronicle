import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { installErrorRelay } from "./errorRelay";

// Pipe console.error/warn, uncaught errors and unhandled rejections to the dev
// server (/__log → ./captures/debug.log) so swallowed errors (e.g. the XR
// controller-layout load that fails via `.catch(console.error)`) are visible.
installErrorRelay();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
