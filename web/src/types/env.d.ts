/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE: string
  readonly VITE_MQTT_WS: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
