import { createConnection } from "node:net";

export type AgentOptions = {
  cwd?: string;
  model?: string;
};

export type Agent = {
  run<TOutput = string>(
    prompt: string,
    options?: AgentOptions,
  ): Promise<TOutput>;
};

type AgentCallRequest = {
  protocol_version: 1;
  type: "agent.call";
  call_id: string;
  prompt: string;
  model: string;
  cwd: string;
  deadline_ms: number;
};

type AgentCallResponse = {
  protocol_version: 1;
  type: "agent.result";
  call_id: string;
  status: "ok" | "error" | "timeout";
  text: string;
  error_code: string;
};

function brokerEndpoint(): { host: string; port: number } {
  const host = process.env.DYRO_BROKER_HOST ?? "127.0.0.1";
  const port = Number(process.env.DYRO_BROKER_PORT ?? "7421");
  if (!host || !Number.isInteger(port) || port <= 0 || port > 65535) {
    throw new Error("DYRO_BROKER_HOST/PORT is invalid");
  }
  return { host, port };
}

function callBroker(request: AgentCallRequest): Promise<AgentCallResponse> {
  return new Promise((resolve, reject) => {
    const { host, port } = brokerEndpoint();
    const client = createConnection({ host, port });
    let buffer = "";
    const timer = setTimeout(() => {
      client.destroy();
      reject(new Error(`broker call timed out: ${request.call_id}`));
    }, request.deadline_ms + 250);

    client.setEncoding("utf8");
    client.on("connect", () => {
      client.write(`${JSON.stringify(request)}\n`);
    });
    client.on("data", (chunk: string) => {
      buffer += chunk;
      const newline = buffer.indexOf("\n");
      if (newline === -1) {
        return;
      }
      const line = buffer.slice(0, newline).trim();
      clearTimeout(timer);
      client.end();
      try {
        const payload = JSON.parse(line) as AgentCallResponse;
        if (
          payload.protocol_version !== 1 ||
          payload.type !== "agent.result" ||
          payload.call_id !== request.call_id
        ) {
          reject(new Error("broker response failed schema checks"));
          return;
        }
        resolve(payload);
      } catch (error) {
        reject(error);
      }
    });
    client.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
  });
}

/**
 * Agent implementation that only talks to the host Broker over a Unix socket.
 * No provider credentials exist in the Workflow Sandbox.
 */
export class BrokerAgent implements Agent {
  #sequence = 0;

  async run<TOutput = string>(
    prompt: string,
    options: AgentOptions = {},
  ): Promise<TOutput> {
    if (!prompt || typeof prompt !== "string") {
      throw new TypeError("BrokerAgent requires a non-empty prompt");
    }
    this.#sequence += 1;
    const request: AgentCallRequest = {
      protocol_version: 1,
      type: "agent.call",
      call_id: `call-${this.#sequence}`,
      prompt,
      model: options.model ?? "fake-model",
      cwd: options.cwd ?? "/worktrees/docs",
      deadline_ms: 3_000,
    };
    const response = await callBroker(request);
    if (response.status !== "ok") {
      throw new Error(
        `broker call failed: ${response.status}:${response.error_code || "unknown"}`,
      );
    }
    return response.text as TOutput;
  }
}

export function bindAgent(runtime: Agent) {
  return <TOutput = string>(
    prompt: string,
    options?: AgentOptions,
  ): Promise<TOutput> => runtime.run<TOutput>(prompt, options);
}
