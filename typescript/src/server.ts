import { createServer } from "node:http";

const PORT = Number(process.env.PORT ?? 8080);
const BODY = "Hello World";

const server = createServer((req, res) => {
  if (req.method === "GET" && req.url === "/api/hello") {
    res.writeHead(200, {
      "Content-Type": "text/plain; charset=utf-8",
      "Content-Length": Buffer.byteLength(BODY),
    });
    res.end(BODY);
    return;
  }
  res.writeHead(404).end();
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(`typescript/node-http listening on :${PORT}`);
});
