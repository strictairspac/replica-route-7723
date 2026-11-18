import {buildsApi} from '../api/builds.js';

export const buildsStore = {
  items: [],
  async refresh() { this.items = await buildsApi.list(); return this.items; },
};
