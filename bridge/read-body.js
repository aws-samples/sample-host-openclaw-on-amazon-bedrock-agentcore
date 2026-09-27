/**
 * Read an HTTP request body as a UTF-8 string.
 *
 * Chunks are collected as Buffers and decoded once at the end. Decoding each
 * chunk on its own (`body += chunk`) turns a multi-byte character that spans
 * two chunks into U+FFFD replacement characters.
 *
 * @param {import("http").IncomingMessage} req
 * @param {number} [maxBytes=Infinity] - Byte cap. Over it, the promise rejects
 *   with an Error whose `code` is "BODY_TOO_LARGE" and the request stops being read.
 * @returns {Promise<string>}
 */
function readBody(req, maxBytes = Infinity) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    let done = false;
    req.on("data", (chunk) => {
      if (done) return;
      size += chunk.length;
      if (size > maxBytes) {
        done = true;
        const err = new Error("Request body too large");
        err.code = "BODY_TOO_LARGE";
        reject(err);
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => {
      if (done) return;
      done = true;
      resolve(Buffer.concat(chunks).toString("utf8"));
    });
    req.on("error", (err) => {
      if (done) return;
      done = true;
      reject(err);
    });
  });
}

module.exports = { readBody };
