import {BUILD_STATES, RUN_STATES} from '../../constants/enums.js';
import {esc} from '../../utils/html.js';

export const badge = (value, labels = RUN_STATES) =>
  `<span class="badge ${esc(value)}">${esc(labels[value] || value)}</span>`;

export const buildBadge = value =>
  `<span class="badge ${value === 'ready' ? 'completed' : value === 'failed' ? 'failed' : 'running'}">${esc(BUILD_STATES[value] || value)}</span>`;
