import { createPromiseClient } from "@connectrpc/connect";
import { createConnectTransport } from "@connectrpc/connect-web";
import { GeoService } from "@/gen/geo/v1/geo_connect";

// Connect transport pointing to the Go Connect microservice
const transport = createConnectTransport({
  baseUrl: process.env.NEXT_PUBLIC_GEO_SERVICE_URL || "http://localhost:8085",
});

export const geoClient = createPromiseClient(GeoService, transport);
