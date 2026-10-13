/**
 * Bootstrap a reserved session-diff worker without dispatching a task.
 *
 * After checking process identity and source hashes, import the pinned Git
 * worker while intercepting its task listener. A private control port handles
 * the generation-bound resume handshake; a shared atomic gate separately
 * requires the owner to claim the worker before task dispatch can begin.
 * The first dispatched task must be checkout.baseline. Module readiness and
 * resume acknowledgements therefore describe preparation, not completed work.
 */
import fs from "node:fs";
import { createHash } from "node:crypto";
import { parentPort, workerData, threadId, isMainThread } from "node:worker_threads";

const protocol = "openclaw.session-diff-worker-control.v1";
const vendor = "/usr/local/lib/node_modules/openclaw/dist/";
const check = (value, reason) => { if (!value) throw new Error(reason); };
const uuid = value => typeof value === "string" &&
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
const pins = [
  ["abort-signal-B6NEzqVq.mjs", "8a45844526a1e262c8e9e29f69abf5f22da352973f829ab0a4a50411f9723e72"],
  ["ansi-CWsy0bu4.mjs", "c8f9b33a0f15e43e5be749beff2bcf8b185c1616f3f1bc18484663c6444ac948"],
  ["assistant-error-format-CUiP1A9G.mjs", "d7419b326f7bcc507532dc24021f7694e26a3e9c2b3dd30af764e5cdb44ba723"],
  ["backoff-CszdOMiF.mjs", "123643c133cf23e4d8ce8805be58cfdd6492be764975072a67f7e35291ccf032"],
  ["boundary-file-read-CnNOMzxg.mjs", "f5b712bcd5690a8cfae48a454514345dc022c5e51231d6ffd4977b27e1cc6fac"],
  ["boundary-path-BI-dkW5f.mjs", "894ba0e2d7da1b41949d51efd4638cc67e1f7e1f05084cd70e7168b3740acf8c"],
  ["child-CO0ygZ0v.mjs", "97c79e28ff941c29c4d3b824930d198ad278009d04b890127fc6c89b6049d3ab"],
  ["child-process-tree-k3mAaiGo.mjs", "337245d458f6ee32a33e77860d7fe19a00825bc0e41d6a0b77bc606640547255"],
  ["cjk-chars-6ld30jSx.mjs", "684e5efca72e3fefeb81c0adebcd0770632c574088da9b9e2a29022dac152cb9"],
  ["command-error-CLRWADNl.mjs", "5052db8b6b171bb6c8ac6f50cc19eaecf41928188211d76bef0613117d8f0ed6"],
  ["console-DJiwXDs9.mjs", "2410d57cd82a638fc47c75c3105a6666e669c4fce2a210363930fdc0d397e332"],
  ["constants-CJCmIHb-.mjs", "f4f7848435497fd132340d50451e812d3fafb6868dbd0dcdddbc2a3c51f82b49"],
  ["context-DoM5VPJ2.mjs", "d568bd69110f6b20cf412e032fa993320c3726a219faa3582be28d94aef20192"],
  ["deep-merge-CthyICF6.mjs", "bcaba56b8b7e563719e63c93aca5b6f6c93c86390814e2f8605ad6c4d07144fd"],
  ["deferred-D0La5CRk.mjs", "e1ea2c9f3a12e70d6a62ecb61107cd43434d06718ff02e719577d6e623476188"],
  ["diagnostic-events-Cpkvonxc.mjs", "b4193c42449558bfb479ea0b421e27a575d43fb1d99c4ec03af58380cac13013"],
  ["dot-path-BSC76DAI.mjs", "13b6191b9f767bafa1003fd1ec37bbf17b39da860e499a8077e38efca6a945a8"],
  ["errno-CkbDOfLk.mjs", "4f3aa2abbb8cb32e20aa0e844d10c41bfb1667ecf34f82a29c3c3b27e9a34089"],
  ["error-coercion-9cgpvoEN.mjs", "7ea274d75d99c8df7712319b356deb61435508b07530c89f0cc5d2ebe74efb94"],
  ["errors-CaVNZEd2.mjs", "eed977d330a819361706aec1731f5dcc4214292158807f960a9aaad9cf98f85a"],
  ["exec-BssZZk55.mjs", "fc5272ba4238b4665eed5fbffcb9e7cb09130429c580ef5cf23d28d2fbee3b1c"],
  ["exec-termination-BwuYp4mb.mjs", "f73b98e2b2463ee9c15a3463830d538a5f8b3998b6cdba3965e12353d613148c"],
  ["executable-path-BMRdcL7i.mjs", "d7bf3308784a7ce6f72ba5b5c1a3ebb8e3a59b3f903eef48759fc7790d9c9dac"],
  ["expect-CyE8FADM.mjs", "3a9546b99658b39d4ca35377019bd8044cb390c87aebeaa53378a766ec132847"],
  ["fixed-window-rate-limit-wyPr2JvV.mjs", "38f5571bb2f70f2fb02de306b1f69645a8b38d3cb14284c193ab1afac364b45a"],
  ["fs-safe-B9ttgVH9.mjs", "c2df2b8f55a22fc347dfbdd044d8146489cf0a9ceebf2b998eb7e0b15c189941"],
  ["fs-safe-advanced-Dj2yJO5t.mjs", "814fbc8b15fefc9aedfa7c9b0a77dbb705089e5c72eb4f854989c2d9edc8476d"],
  ["fs-safe-defaults-2jt53BPm.mjs", "66dcddde03c1a2548f298d91f4cef22c0daa58521ea26fdf28948bdca57acdf8"],
  ["git-C1wmUsUU.mjs", "682d4498ea774bbf665cf93728c1d87aa0536a3c3e88bc1f94273dddd622cbcb"],
  ["git-exec-DcfGCJzK.mjs", "ff739b55a75eeda2eb189420f0af180225334b646b05c6c8b851b6ca98604965"],
  ["git-worker-context-DYIo0ClU.mjs", "577851f90c7f98a8fad73b1bc22893ac2741197a3683bb6eac27a04151963b56"],
  ["github-remote-CloOx2kv.mjs", "058b5ee39f2730c7c95fd2b70d1ce43bae4905ef5d41e60d1ff7f23266ec2c4d"],
  ["global-singleton-Dc_stLtU.mjs", "aa27b41a8ee873d070aeca8a79f28439f82ac0d458b9016040ab07f89516d089"],
  ["global-state-BAD7XgmL.mjs", "3dc2c6af9753ad39869fba7e5475e670790e5334b9b57cb72b36782acb50f8ac"],
  ["home-dir-BKwhAL2c.mjs", "6de8961fdf3eb6ed8d8750d82f8073a6a0b57d93e812b273496f612ad12b1293"],
  ["home-dir-D0b-ywue.mjs", "5fcab562e46b4b88dfea7d630ea621eee80bd6185e52895e6b08befa4d2e4a3c"],
  ["includes-agM75dKv.mjs", "105ead18b76efebb1da15f499b4892d2c522e6e2c5e3c8be58d03692b8c79dc9"],
  ["infra/git-operation.worker.js", "6bbd35a20f7edaf0745c84bc8b23edb5f569e419dcef019f8c41c19eb4923546"],
  ["json-console-line-CkAqi7Mz.mjs", "4bfd49f95e101563b6af5006cef5b05a527da05f9eb38721b2321c3ef36e86a6"],
  ["keyed-async-queue-CTreGrmR.mjs", "5e527842e6889f3addfd7faaffc05e461f638c4ec3e1d818fb3f775407a4d34e"],
  ["kill-tree-BGQdx374.mjs", "ed58abfc6458094cb63032a53cf646cac2328b7ef590a753c31ac7511bcc778b"],
  ["levels-qpAN12Fm.mjs", "67b124c75a3ce3ee37dd38a225eccaa9c6c2dad1c8dc4c5b80f66605bc404f8e"],
  ["logger-C1xe8T4P.mjs", "bcb3128e2eb59bf1f3bae9f241e2eb2d45de54c9e9564729c1f8ef1d45d2cdf8"],
  ["map-size-CNcWiFKu.mjs", "4805eef393e6404cf6f0589159f1b9d442e96f112961f8f27d81fac7194fe309"],
  ["non-fatal-cleanup-BoCq8AND.mjs", "7adbdea9e1d2724519bbfb0b6efa74fcac3dd601a4d28d244ca4e75f5ec2b452"],
  ["number-coercion-CLj0HTDM.mjs", "0ed7f5c11b0e6b4cd307532bbe46b709dbdbb09eaa584e57e2edbfb44689aa08"],
  ["openclaw-exec-env-Cc7ECPuD.mjs", "537a495d48b22a941f907f453587ab316415b4dd1116fd09a582f252c9945ab2"],
  ["parse-json-compat-BBtWoq5_.mjs", "fbd05d3ec8d3ad92c473d16e56c7bad54851f336947cef2e4179b34489b0eefa"],
  ["path-array-index-CvEcUJa-.mjs", "d7b1e9f74382c6fa3a45923691f68a6f648b3de2e844019796ca34f87d71066c"],
  ["path-guards-CmrFoD5i.mjs", "7c3c0cc061b142d4f94800f9dcf06d68b36f542fdf549091098a6d0e0675eed8"],
  ["paths-COxaSNLm.mjs", "4450b87c531a80df8a3302074a2c344569a55f4067b8774c637ea90b66c61a15"],
  ["pid-alive-CXdZEzr_.mjs", "d407041835d5c89a00933b039328c4dc08e28bcbe535e204a514c77ba5195e1a"],
  ["plain-object-5a0EzLzX.mjs", "0091a805386fd77c9427afcbb2bb60bce4ce4061f2248f852a5717e2b9ef3841"],
  ["process-env-DlZFJzq6.mjs", "9dbcbc744454579b01e659a11cebe05e22b9be819a7b2292c8c1ed388fe2b768"],
  ["profile-utils-D5FwriYc.mjs", "7449d4cde68f850434ffc0aa3be552665c8134f0679000d206a8c3dd54d8c27c"],
  ["progress-line-DiTuCPbL.mjs", "1a421976aa475eb1918300942179ef806c127bb8b992ea5336a5da1f610b13f2"],
  ["promise-like-D7-l5Fsp.mjs", "2f7e83c80d9a96a5bd72f605abcf9ee02fc5af8b371e30b96a86ad4146b30c51"],
  ["prototype-keys-CuYw53fZ.mjs", "b7a3226a0b3fe908491b56ea1f17f266801e7172c88e21160c6c88b18b6fff09"],
  ["record-coerce-DItp3I4t.mjs", "e5acf8ca52794e3fce5b7db301d2425290a09dac816d3965c774f94b8b934dfe"],
  ["redact-CrCCqliq.mjs", "2cdc2b7677c30e12e3d4d29da70bd8dd288de17c5c15f08ff68dde4ea5b89a44"],
  ["redact-sensitive-url-DspuXlEe.mjs", "bbb9977cea956d8f4770b3f30cfe0860dce1ad89bb8705b48a0f11dd646cff8d"],
  ["ref-contract-CpZCkwB4.mjs", "843a7b433b6dc7d4dd2f77447d8b2064fdfd000a9b05e1a0aea0aaa533bd6182"],
  ["regexp-BZyMFTlj.mjs", "35bc25bc6ec71a81fbd9548a563731c410228d746be3daff57b833ab833632c4"],
  ["regular-file-Chl659cu.mjs", "e8a9cb7e7f7993e1fac759ca83ffe77bd5533bbfce3386e92c32617c558376cc"],
  ["repository-paths-HVPQ_BOH.mjs", "a9028020bf95a93a3515de759ebee12ad6b224d9139e7bcb17f4c1aa57f14e96"],
  ["restore-CKYlAJHw.mjs", "e4f61035c743ecfc6b0571d454f82220086f58da24f49c685f136f2b61661a83"],
  ["runtime-KtFRxdRg.mjs", "53209c88a1ab5d84036e8735a66f644f979e03f0f406079d952ac6ebd4843957"],
  ["runtime-binary-Cy5Lhult.mjs", "fba95cec84a5d4651f8bd19b49b1310b3ca3944c8a1d517f445ff9fa938c5e11"],
  ["runtime-process-entrypoints-CLuFQvrX.mjs", "a74a1edb84c1216483e533997976b1f2babbf34487db210439392b7a0f45b75b"],
  ["runtime-worker-url-B4o9tqvO.mjs", "9984479ee3c734ff5a25c63e73b81287e19f5031877e1533c49fd90cc9439a70"],
  ["safe-cwd-DOxDm8mD.mjs", "bec1a7e4e730003da9c757012afd3cd3e95ff6f768a29553ef62913ea3e42e66"],
  ["safe-text-CBmKtmbt.mjs", "3b9de9b0a942a3c2d7a742fd40fbd3ad1f16a8f2378502c116a17b137c7f8def"],
  ["scoped-child-reaper-GgqPcup9.mjs", "61a5cab805003485d291b2154bed96bcf081ab1753677e090100e796dba6f051"],
  ["sealed-runtime-registry-DDnWdfsn.mjs", "1eb8e3dc7ae02749903e6c35cf5e215b2a50c13a606d9b9767ed45a1bfb8e2e6"],
  ["secret-redaction-registry-DMqOFY3S.mjs", "1cb82c277579d5093d026deff16fd8c00db4c50c3937a1980dff9ebe4fdb51df"],
  ["src-CZ2wJvNB.mjs", "ad47ecb3ec264c3c7fc548f81652de1d81b766b82ad8b63027c68b4cebed64fd"],
  ["src-D4OikzaT.mjs", "c22200cfb184288abcd4483eab1d2223223ce4ca5ffe2565caae51a2dcf2a3d7"],
  ["staged-inputs-CPTaFQ1p.mjs", "3d2d57423693c5b9df00cc08938ddbab080df3f397ea4577c4607f70fde473ac"],
  ["state-UxwJeH7V.mjs", "0ed917c0a304b2c2d79219a19bbb9eb488e341b913c37340b9cdcca210b35742"],
  ["string-coerce-CIXf7egm.mjs", "db70aa59f984a63935d147b6ac11b50a90f21b726998853ef3455451f1e622e7"],
  ["structured-auth-redaction-OhcG53GS.mjs", "e1ebcd6a72ff398ca4bf869040030b114baff1985a6c2509a8fd765b0416780d"],
  ["subsystem-De62xgJV.mjs", "42d948102504b6c26b20649b9e19c309eb4d34c16abf75e68fc74a565a25b4fa"],
  ["tcp-port-BVV_ljmK.mjs", "90bd1850e022af48b4266ed397a96dce891744f2050091aa64fa559a7ac70c89"],
  ["test-runtime-env-DQDRzsLt.mjs", "e9000342045431ef87d95881cb707a6f4dcbef0543324adc23c20ea066d1e98a"],
  ["timestamps-tLN31L4a.mjs", "040a9c3deb16a530added9bf0484dc3b52c89be3fe832d1f9cf86cfe12da9725"],
  ["tmp-openclaw-dir-BSb3HetY.mjs", "b5f3f4cfc8211fe003211a12b08d0087c0ec7bcd65969b7ab5c84b0cb49d47c9"],
  ["types.secrets--cg75zwv.mjs", "a478d6b3cf17228ab71a0b9dfb453597b8741a1fda604834fffe5b1b6e48b483"],
  ["utf16-slice-D_ngcYKd.mjs", "bc9d5d05e504a8c51ede86fbda75f5079388e50e42ac6ecd3885146f0193e944"],
  ["utf8-truncate-_hf7tp13.mjs", "40a2cf5ddbff0519fd46a7c3453df44476a4da280f11e8892988449f5f8cbd56"],
  ["utils-5NRg_vUS.mjs", "c219eecd2c3a6b88ca3ac361e461f61d5eb48abd1492b022fe3ae4c0a2cd1584"],
  ["windows-command-CvM0L-Gp.mjs", "7c773de83c072251d8388defd8c54d34a1ca9d1b5632d95995c4a19c6bf04e84"],
  ["windows-encoding-zGMz1ieM.mjs", "b0dd94268c0eb70e0fb1a09146c1e831e7716ba102d00a8addab0a74ecfa7f66"],
  ["windows-install-roots-DK9gNoYN.mjs", "f261ebbf5fabaa3c41e873ebe74957df665e88916251f2e26862eedbfd62fcd2"],
  ["worker-task-pool-Y38sR9E6.mjs", "9af05c31d897597c17adef77bd0291b271463d1dbdc3c97c13d2b779e6a43585"],
  ["worker-transfer-bytes-D_DP0IHa.mjs", "f94bcd5f3073f36b37cb557fcc50af24a19f8d91f25c7b1881f6c171550fa849"],
];
check(!isMainThread && parentPort && workerData?.schema === protocol &&
  Object.keys(workerData).sort().join(",") ===
    "control,gate,gatewayPid,gatewayStart,preparationId,schema,seedId" &&
  uuid(workerData.seedId) && uuid(workerData.preparationId) && workerData.gatewayPid === process.pid &&
  typeof workerData.gatewayStart === "string" && /^\d+$/.test(workerData.gatewayStart) &&
  workerData.gate instanceof SharedArrayBuffer && workerData.gate.byteLength === Int32Array.BYTES_PER_ELEMENT &&
  workerData.control?.constructor.name === "MessagePort", "session_diff_worker_bootstrap_identity");
const stat = fs.readFileSync("/proc/self/stat", "utf8");
check(stat.slice(stat.lastIndexOf(")") + 2).split(" ")[19] === workerData.gatewayStart,
  "session_diff_worker_bootstrap_identity");
const identity = Object.freeze({ seedId: workerData.seedId, gatewayPid: process.pid,
  gatewayStart: workerData.gatewayStart, preparationId: workerData.preparationId });
const control = workerData.control, gate = new Int32Array(workerData.gate);
check(Atomics.load(gate, 0) === 0, "session_diff_worker_bootstrap_gate");
let generation = null, dispatches = 0, resumed = false, taskListener;
const originalOn = parentPort.on;
parentPort.on = function (event, listener) {
  if (event === "message") {
    check(!taskListener && typeof listener === "function", "session_diff_worker_listener_shape");
    taskListener = listener;
    return this;
  }
  return originalOn.call(this, event, listener);
};
function reply(event, requestId, extra = {}) {
  control.postMessage({ schema: protocol, event, requestId, ...identity, generation,
    status: "ok", threadId, gate_state: Atomics.load(gate, 0), task_dispatch_count: dispatches, ...extra });
}
const started = process.hrtime.bigint();
try {
  check(JSON.parse(fs.readFileSync(vendor + "../package.json", "utf8")).version === "2026.9.5",
    "session_diff_worker_version");
  check(pins.length > 0, "session_diff_worker_missing_pins");
  for (const [file, sha256] of pins) {
    const path = vendor + file, info = fs.lstatSync(path);
    check(info.isFile() && !info.isSymbolicLink() &&
      createHash("sha256").update(fs.readFileSync(path)).digest("hex") === sha256,
    "session_diff_worker_import_pin");
  }
  await import(vendor + "infra/git-operation.worker.js");
} finally {
  parentPort.on = originalOn;
}
check(taskListener && parentPort.listenerCount("message") === 0, "session_diff_worker_listener_missing");
parentPort.on("message", function (message) {
  check(resumed && Atomics.load(gate, 0) >= 2, "session_diff_worker_task_before_claim");
  if (dispatches === 0) {
    check(Atomics.compareExchange(gate, 0, 2, 3) === 2 && message?.input?.type === "checkout.baseline" &&
      message.responseId === undefined, "session_diff_worker_first_task");
  }
  dispatches++;
  return taskListener.call(parentPort, message);
});
control.on("message", message => {
  check(message && Object.keys(message).sort().join(",") ===
    "event,gatewayPid,gatewayStart,generation,preparationId,requestId,schema,seedId" &&
    message.schema === protocol && message.event === "resume" && !resumed &&
    Object.keys(identity).every(key => message[key] === identity[key]) &&
    uuid(message.generation) && uuid(message.requestId) &&
    Atomics.compareExchange(gate, 0, 0, 1) === 0, "session_diff_worker_resume_identity");
  generation = message.generation; resumed = true;
  reply("resume_ready", message.requestId);
});
const ended = process.hrtime.bigint();
reply("module_ready", identity.preparationId, { task_listener_count: 1,
  module_span: { pid: process.pid, thread_id: threadId, start_ns: started.toString(),
    end_ns: ended.toString(), duration_ns: (ended - started).toString(),
    boundary: "pin_validation_and_full_import_to_listener_ready" } });
