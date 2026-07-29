const child = Bun.spawn(["sh", "-c", "sleep 60"], {
  stdin: "ignore",
  stdout: "ignore",
  stderr: "ignore",
});

await child.exited;
