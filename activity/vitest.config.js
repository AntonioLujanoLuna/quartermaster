import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // The screens are built with createElement against a real document, so the
    // test needs one. Nothing here talks to the network: render.js imports only
    // format.js, which is why it can be exercised without a bot behind it.
    environment: "jsdom",
    include: ["tests/**/*.test.js"],
  },
});
