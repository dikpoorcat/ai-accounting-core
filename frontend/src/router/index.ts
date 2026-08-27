import { createRouter, createWebHistory } from "vue-router";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: "/",
      name: "brief",
      component: () => import("../views/BriefView.vue"),
    },
    {
      path: "/funds",
      name: "funds",
      component: () => import("../views/FundsView.vue"),
    },
    {
      path: "/employees",
      name: "employees",
      component: () => import("../views/EmployeesView.vue"),
    },
    {
      path: "/assets",
      name: "assets",
      component: () => import("../views/AssetsView.vue"),
    },
    {
      path: "/reports",
      name: "reports",
      component: () => import("../views/ReportsView.vue"),
    },
    {
      path: "/:pathMatch(.*)*",
      redirect: { name: "brief" },
    },
  ],
});

export default router;
