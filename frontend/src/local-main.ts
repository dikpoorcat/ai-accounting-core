import { createApp } from "vue";

import { consumeLocalToken } from "./api/localKernel";
import LocalKernelView from "./views/LocalKernelView.vue";
import "./styles.css";

consumeLocalToken();
createApp(LocalKernelView).mount("#app");
