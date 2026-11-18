/** Build:{id,name,table_name,index_name,key_column,policy,batch_size,status,initial_state,events,state,summary,error}
 * Node: {name,role,status,position,delay_ms,index_state,installed_position}
 * Row:  {id,value,version,deleted,updated_seq}
 * Entry:{row_id,key_value,version,source,batch_no,seq,state}
 * Job:  {id,build_id,status,expected_checksum,actual_checksum,event_count,artifact,error}
 */
export const entityNames=['index_builds','build_jobs'];
