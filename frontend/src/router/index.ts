import { createRouter, createWebHistory } from "vue-router";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: "/review/materials",
      name: "review-materials",
      meta: { requiresPeriod: false },
      component: () => import("../views/ReviewMaterialsView.vue"),
    },
    {
      path: "/company-note",
      name: "company-note",
      meta: { requiresPeriod: false },
      component: () => import("../views/CompanyNoteView.vue"),
    },
    {
      path: "/",
      name: "brief",
      meta: { requiresPeriod: true },
      component: () => import("../views/BriefView.vue"),
    },
    {
      path: "/funds",
      name: "funds",
      meta: { requiresPeriod: true },
      component: () => import("../views/FundsView.vue"),
    },
    {
      path: "/employees",
      name: "employees",
      meta: { requiresPeriod: true },
      component: () => import("../views/EmployeesView.vue"),
    },
    {
      path: "/assets",
      name: "assets",
      meta: { requiresPeriod: true },
      component: () => import("../views/AssetsView.vue"),
    },
    {
      path: "/reports",
      name: "reports",
      meta: { requiresPeriod: true },
      component: () => import("../views/ReportsView.vue"),
    },
    {
      path: "/:pathMatch(.*)*",
      redirect: { name: "brief" },
    },
  ],
});

export default router;
