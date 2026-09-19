export type RecordData = Record<string, unknown>;
export interface SetupCandidate { id:string; name:string; path:string; evidence?:string[]; valid?:boolean; }
export interface SetupInventoryItem { id?:string; name?:string; label?:string; aircraft?:string|string[]; path?:string; type?:string; guid?:string|null; connection?:string; connected?:boolean|null; evidence?:string[]; diff_files?:Array<{aircraft:string;path:string}>; }
export interface SetupBinding { aircraft:string; label?:string; status:string; counts?:Record<string,number|null>; reason?:string; display_only?:boolean; }
export interface SetupState {
  status:'Ready'|'Needs attention';
  selected:{install_path?:string|null;profile_path?:string|null;install_id?:string|null;profile_id?:string|null};
  install_candidates:SetupCandidate[];
  profile_candidates:SetupCandidate[];
  issues:string[];
  inventory:{installed_aircraft?:Array<SetupInventoryItem|string>;saved_aircraft?:Array<SetupInventoryItem|string>;devices?:Array<SetupInventoryItem|string>;counts?:Record<string,number>};
  active?:{install_path?:string|null;profile_path?:string|null};
  pending_restart:boolean;
  selection_locked?:boolean;
  bindings?:SetupBinding[];
  assignment_policy:'read-only';
}
export interface GuideAction { id: string; title: string; purpose?: string; binding?: unknown; location?: string; expected_result?: string; observable?: boolean; risk?: string; enabled?: boolean; reason?: string; observed_state?: unknown; current_observed_state?: unknown; is_toggle?: boolean; }
export interface QueueState { state?: string; request_id?: string; action_id?: string; detail?: string; history?: RecordData[]; dry_run?: boolean; expires_at?: string | number; }
export interface Snapshot {
  smart?: {status?:string;title?:string;detail?:string;automatic?:boolean;binding_refresh?:{status?:string;detail?:string};navigation_refresh?:{status?:string;detail?:string;generation?:number};autostart?:{enabled?:boolean;status?:string;state?:string;last_error?:string|null}};
  server?: { remote_armed?:boolean;remote_mode?:'preview'|'live'; dry_run?: boolean; lan_enabled?: boolean; connected_clients?: number; version?: string };
  health?: { status?: string; aircraft?: string; terrain?: string; mission?: string; session?: string; telemetry_age_s?: number; display_age_s?: number; telemetry_fresh?: boolean; model_advancing?: boolean; displays_fresh?: boolean; dcs_running?: boolean; dcs_focused?: boolean; export_capabilities?: unknown; sensor_status?: string; physical_input_status?: string; binding_status?: string; notes?: string[]; };
  aircraft?: RecordData;
  bindings?: RecordData;
  binding_catalogs?: Array<{aircraft:string;label?:string;status?:string;source_fingerprint?:string;generated_utc?:string;generation?:number}>;
  sensors?: RecordData;
  cockpit?: RecordData;
  mission?: {status?:string;name?:string|null;theatre?:string|null;source?:unknown;age_s?:number|null;detail?:string};
  export_data?: RecordData;
  adapters?: RecordData[];
  navigation?: RecordData;
  awareness?: RecordData;
  guide?: { actions?: GuideAction[]; mode?: string };
  queue?: QueueState;
}
