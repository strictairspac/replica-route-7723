import {api} from './client.js';

export const buildsApi = {
  list: () => api('/builds'),
  detail: id => api('/builds/' + id),
  create: data => api('/builds', 'POST', data),
  append: (id, events) => api(`/builds/${id}/events`, 'POST', {events}),
  rewind: (id, keep) => api(`/builds/${id}/rewind`, 'POST', {keep}),
  verify: id => api(`/builds/${id}/verify`, 'POST', {}),
  jobs: id => api('/jobs?build_id=' + id),
};
