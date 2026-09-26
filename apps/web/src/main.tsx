import { render } from "preact";

import { App } from "./app";
import "./styles.css";

const root = document.getElementById("app");
if (root === null) {
  throw new Error("Missing #app root element");
}
render(<App />, root);

if ("serviceWorker" in navigator && import.meta.env.PROD) {
  navigator.serviceWorker.register("/sw.js").catch(() => {
    // Offline shell is a convenience; the app works without it.
  });
}
