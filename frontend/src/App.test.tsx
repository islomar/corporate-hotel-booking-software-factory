import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";

describe("App", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows the backend status when the health check succeeds", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify({ status: "ok", version: "test" })),
        ),
    );

    render(<App />);

    expect(await screen.findByRole("status")).toHaveTextContent(
      "Backend status: ok (version test)",
    );
  });

  it("shows an error when the health check fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("", { status: 503 })),
    );

    render(<App />);

    expect(await screen.findByRole("alert")).toHaveTextContent("HTTP 503");
  });
});
