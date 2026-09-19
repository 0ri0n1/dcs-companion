export interface RemoteButton {id:string;name:string;label?:string;available:boolean;reason?:string;combo?:unknown;}
export interface RemotePanel {id:string;label:string;buttons:RemoteButton[];}
export type WorkflowStep={type:'action';action_id:string}|{type:'delay';seconds:number}|{type:'wait';display_id:string;name:string;equals:string;timeout_s:number}|{type:'mission_script';script_id:string};
export interface Workflow {id:string;name:string;aircraft:string;steps:WorkflowStep[];}
export interface RemoteRun {id:string;status:string;mode?:string;step_index:number;step_count:number;message?:string;events?:Array<{status?:string;message?:string;detail?:string}>;}
export interface RemoteScript {id:string;name?:string;description?:string;available?:boolean;reason?:string;approved?:boolean;approved_revision?:string;sha256?:string;workflow_id?:string;aircraft?:string[];context?:string;status?:string;single_player_only?:boolean;}
export interface RemoteState {status:string;mode:'preview'|'live';armed:boolean;arm_expires_at?:number;heartbeat_timeout_s?:number;controller_connected:boolean;can_edit?:boolean;can_arm?:boolean;busy:boolean;active_run?:RemoteRun|null;last_run?:RemoteRun|null;aircraft?:string;reason?:string;panels:RemotePanel[];workflows:Workflow[];scripts:RemoteScript[];}
