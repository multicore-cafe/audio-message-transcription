const fs = require("fs/promises");

const httpProxy = require("http-proxy");

async function main() {
  httpProxy
    .createProxyServer({
      target: { host: "localhost", port: 3003 },
      ssl: {
        key: await fs.readFile("./voicesanitizer.localhost-key.pem", "utf8"),
        cert: await fs.readFile("./voicesanitizer.localhost.pem", "utf8"),
      },
    })
    .listen(3004);
}
main();
