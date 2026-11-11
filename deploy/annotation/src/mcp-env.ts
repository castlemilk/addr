/** The companion gateway has no container, database or private asset bindings. */
export interface McpEnvironment extends Omit<HostedMcpEnv, "OAUTH_GRANTS" | "FIELDWORK_SHARED_WORKSPACES" | "FIREBASE_PROJECT_ID"> {
  OAUTH_GRANTS: DurableObjectNamespace;
  ANNOTATION_TOKEN: string;
  FIELDWORK_SHARED_WORKSPACES?: string;
  FIREBASE_PROJECT_ID?: string;
  FIREBASE_API_KEY?: string;
  MCP_ORIGIN?:string;
  APPLICATION_ORIGIN?:string;
}

/** The SDK adapter needs only an authenticated path to the existing backend. */
export interface McpBackendEnvironment {
  ANNOTATION_BACKEND: { getByName(name: string): Pick<Fetcher, "fetch"> };
  FIELDWORK_EDITOR_ORIGIN: string;
}
