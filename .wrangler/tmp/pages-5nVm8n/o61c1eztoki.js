// <define:__ROUTES__>
var define_ROUTES_default = {
  version: 1,
  include: ["/"],
  exclude: ["/ko/*", "/en/*", "/assets/*", "/favicon.svg", "/robots.txt", "/sitemap.xml"]
};

// ../../.npm/_npx/d77349f55c2be1c0/node_modules/wrangler/templates/pages-dev-pipeline.ts
import worker from "/Users/swain/workspace/avouch/.wrangler/tmp/pages-5nVm8n/functionsWorker-0.2635654244897552.mjs";
import { isRoutingRuleMatch } from "/Users/swain/.npm/_npx/d77349f55c2be1c0/node_modules/wrangler/templates/pages-dev-util.ts";
export * from "/Users/swain/workspace/avouch/.wrangler/tmp/pages-5nVm8n/functionsWorker-0.2635654244897552.mjs";
var routes = define_ROUTES_default;
var pages_dev_pipeline_default = {
  fetch(request, env, context) {
    const { pathname } = new URL(request.url);
    for (const exclude of routes.exclude) {
      if (isRoutingRuleMatch(pathname, exclude)) {
        return env.ASSETS.fetch(request);
      }
    }
    for (const include of routes.include) {
      if (isRoutingRuleMatch(pathname, include)) {
        const workerAsHandler = worker;
        if (workerAsHandler.fetch === void 0) {
          throw new TypeError("Entry point missing `fetch` handler");
        }
        return workerAsHandler.fetch(request, env, context);
      }
    }
    return env.ASSETS.fetch(request);
  }
};
export {
  pages_dev_pipeline_default as default
};
//# sourceMappingURL=o61c1eztoki.js.map
