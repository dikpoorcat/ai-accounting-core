import { createApp } from "vue";

import App from "./App.vue";
import { consumeLocalTicket, localErrorMessage } from "./api/localKernel";
import "./styles.css";

async function start() {
  let launchError = "";
  try { await consumeLocalTicket(); }
  catch (error) { launchError = localErrorMessage(error); }
  // Create router history only after launch credentials have been removed.
  const { default: router } = await import("./router");
  const app = createApp(App, { launchError }).use(router);
  await router.isReady();
  app.mount("#app");
}
void start();
