const { defineConfig } = require('cypress');

module.exports = defineConfig({
  video: false,
  allowCypressEnv: false,
  viewportWidth: 390,
  viewportHeight: 900,
  defaultCommandTimeout: 10000,
  e2e: {
    baseUrl: 'https://127.0.0.1:8443',
    supportFile: false,
    specPattern: 'cypress/e2e/*.cy.js',
  },
});
