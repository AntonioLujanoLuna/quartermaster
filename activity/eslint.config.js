// What the production build cannot tell anyone.
//
// CI already builds the bundle, and a build proves the module graph parses and
// resolves. It does not notice a handler that is never called, a variable that
// shadows the one the line below meant, or a promise nobody awaited — and those
// are the failures that reach a table mid-evening rather than a pull request.
//
// Formatting is Prettier's, not this file's: `eslint-config-prettier` turns off
// every stylistic rule so the two never disagree about the same line.

import js from "@eslint/js";
import globals from "globals";
import prettier from "eslint-config-prettier";

export default [
  { ignores: ["dist/**", "node_modules/**"] },
  js.configs.recommended,
  {
    files: ["src/**/*.js"],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "module",
      globals: globals.browser,
    },
    rules: {
      // `_` is how this codebase already names a binding it is required to
      // accept and has no use for, in JavaScript as in the Python.
      "no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
      eqeqeq: ["error", "smart"],
      "no-var": "error",
      "prefer-const": "error",
    },
  },
  {
    files: ["tests/**/*.js", "*.config.js"],
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "module",
      globals: { ...globals.node, ...globals.browser },
    },
  },
  prettier,
];
