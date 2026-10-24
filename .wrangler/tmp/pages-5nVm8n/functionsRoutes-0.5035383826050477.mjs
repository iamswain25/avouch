import { onRequestGet as __index_js_onRequestGet } from "/Users/swain/workspace/avouch/functions/index.js"
import { onRequestHead as __index_js_onRequestHead } from "/Users/swain/workspace/avouch/functions/index.js"

export const routes = [
    {
      routePath: "/",
      mountPath: "/",
      method: "GET",
      middlewares: [],
      modules: [__index_js_onRequestGet],
    },
  {
      routePath: "/",
      mountPath: "/",
      method: "HEAD",
      middlewares: [],
      modules: [__index_js_onRequestHead],
    },
  ]