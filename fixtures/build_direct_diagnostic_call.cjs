// Build a funding transaction or a call with two distinguishable real scripts.
// Python sends both directly to CKB and verifies confirmation; this helper never
// writes OffCKB transaction caches or debugger context.
const fs = require("node:fs");
const { createRequire } = require("node:module");

async function main() {
  const [cliEntry, requestPath, keyOption, keyPath] = process.argv.slice(2);
  if (keyOption !== "--privkey-file" || !keyPath) throw new Error("Expected --privkey-file");
  const load = createRequire(fs.realpathSync(cliEntry));
  const ccc = load("@ckb-ccc/core");
  const { cccA } = load("@ckb-ccc/core/advanced");
  const request = JSON.parse(fs.readFileSync(requestPath, "utf8"));
  const system = request.systemScripts.devnet;
  const client = new ccc.ClientPublicTestnet({
    url: request.rpcUrl, fallbacks: [],
    scripts: {
      [ccc.KnownScript.Secp256k1Blake160]: system.secp256k1_blake160_sighash_all.script,
      [ccc.KnownScript.NervosDao]: system.dao.script,
      [ccc.KnownScript.AnyoneCanPay]: system.anyone_can_pay.script,
    },
  });
  const signer = new ccc.SignerCkbPrivateKey(client, fs.readFileSync(keyPath, "utf8").trim());
  const vm = system.ckb_js_vm.script;
  const scripts = Object.fromEntries(Object.entries(request.contracts).map(([name, contract]) => [name, {
    codeHash: vm.codeHash, hashType: vm.hashType,
    args: "0x0000" + contract.codeHash.slice(2)
      + ccc.hexFrom(ccc.hashTypeToBytes(contract.hashType)).slice(2) + "00".repeat(32),
  }]));
  let tx;
  if (!request.fundingHash) {
    tx = ccc.Transaction.from({ outputs: [{ lock: scripts.target, capacity: 30000000000n }] });
    await tx.completeInputsByCapacity(signer);
    await tx.completeFeeBy(signer, 1000);
    tx = await signer.signTransaction(tx);
  } else {
    tx = ccc.Transaction.from({
      inputs: [{ previousOutput: { txHash: request.fundingHash, index: 0 }, since: 0 }],
      outputs: [{
        lock: (await signer.getRecommendedAddressObj()).script,
        type: scripts.other, capacity: 29900000000n,
      }],
      outputsData: ["0x"], witnesses: ["0x"],
      cellDeps: [
        ...vm.cellDeps, ...request.contracts.target.cellDeps, ...request.contracts.other.cellDeps,
      ].map(({ cellDep }) => cellDep),
    });
  }
  const transaction = cccA.JsonRpcTransformers.transactionFrom(tx);
  for (const output of transaction.outputs) output.type ??= null;
  process.stdout.write(JSON.stringify({ txHash: tx.hash(), transaction, scripts }) + "\n");
}

main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
