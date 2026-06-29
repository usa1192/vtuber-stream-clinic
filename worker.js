const API_ORIGIN = "https://vtuber-stream-clinic.onrender.com";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname.startsWith("/api/")) {
      const upstream = new URL(url.pathname + url.search, API_ORIGIN);
      const headers = new Headers(request.headers);
      headers.delete("host");

      return fetch(
        new Request(upstream, {
          method: request.method,
          headers,
          body: request.body,
          redirect: "follow",
        }),
      );
    }

    return env.ASSETS.fetch(request);
  },
};
