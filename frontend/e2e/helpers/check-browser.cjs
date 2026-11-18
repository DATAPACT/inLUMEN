// Exits with an error when the test browser cannot start, so CI knows it has
// to install the browser's system libraries first.
const { chromium } = require('@playwright/test');

chromium
  .launch()
  .then((browser) => browser.close())
  .catch((error) => {
    console.error(error.message);
    process.exit(1);
  });
