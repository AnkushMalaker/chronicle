var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __export = (target, all) => {
  for (var name in all)
    __defProp(target, name, { get: all[name], enumerable: true });
};
var __copyProps = (to, from, except, desc) => {
  if (from && typeof from === "object" || typeof from === "function") {
    for (let key of __getOwnPropNames(from))
      if (!__hasOwnProp.call(to, key) && key !== except)
        __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
  }
  return to;
};
var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);

// main.ts
var main_exports = {};
__export(main_exports, {
  default: () => ChronicleCompanionPlugin
});
module.exports = __toCommonJS(main_exports);
var import_obsidian = require("obsidian");
var DEFAULT_SETTINGS = {
  baseUrl: "",
  secretName: "",
  dismissedSuggestions: []
};
async function sha256(text) {
  const bytes = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest)).map((byte) => byte.toString(16).padStart(2, "0")).join("");
}
function personName(file) {
  var _a;
  return ((_a = file.parent) == null ? void 0 : _a.path) === "People" ? file.basename : null;
}
var ChronicleCompanionPlugin = class extends import_obsidian.Plugin {
  constructor() {
    super(...arguments);
    this.settings = DEFAULT_SETTINGS;
  }
  async onload() {
    this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
    await this.migrateBootstrapApiKey();
    this.addSettingTab(new ChronicleSettingTab(this.app, this));
    this.addCommand({
      id: "merge-current-person",
      name: "Merge current person\u2026",
      checkCallback: (checking) => {
        const source = this.app.workspace.getActiveFile();
        if (!source || !personName(source)) return false;
        if (!checking) new PersonPickerModal(this, source).open();
        return true;
      }
    });
    this.addCommand({
      id: "review-duplicate-people",
      name: "Review possible duplicate people\u2026",
      callback: () => void this.reviewDuplicatePeople()
    });
  }
  async saveSettings() {
    await this.saveData(this.settings);
  }
  async migrateBootstrapApiKey() {
    if (!this.settings.bootstrapApiKey) return;
    const secretName = "chronicle-companion-api-key";
    this.app.secretStorage.setSecret(secretName, this.settings.bootstrapApiKey);
    this.settings.secretName = secretName;
    delete this.settings.bootstrapApiKey;
    await this.saveSettings();
  }
  apiKey() {
    if (!this.settings.secretName) {
      throw new Error("Choose a Chronicle API key in the plugin settings.");
    }
    const value = this.app.secretStorage.getSecret(this.settings.secretName);
    if (!value) throw new Error("The selected Chronicle API key is empty.");
    return value;
  }
  endpoint(path) {
    const base = this.settings.baseUrl.trim().replace(/\/+$/, "");
    if (!base) throw new Error("Set the Chronicle server URL in the plugin settings.");
    return `${base}${path}`;
  }
  async previewMerge(source, target) {
    const [sourceText, targetText] = await Promise.all([
      this.app.vault.read(source),
      this.app.vault.read(target)
    ]);
    const response = await (0, import_obsidian.requestUrl)({
      url: this.endpoint("/api/memories/people/merge/preview"),
      method: "POST",
      headers: {
        Authorization: `Bearer ${this.apiKey()}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        source_name: source.basename,
        target_name: target.basename,
        source_hash: await sha256(sourceText),
        target_hash: await sha256(targetText)
      })
    });
    return response.json;
  }
  async applyMerge(preview) {
    await (0, import_obsidian.requestUrl)({
      url: this.endpoint("/api/memories/people/merge"),
      method: "POST",
      headers: {
        Authorization: `Bearer ${this.apiKey()}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        source_name: preview.source_name,
        target_name: preview.target_name,
        plan_token: preview.plan_token
      })
    });
  }
  async getSuggestions() {
    const response = await (0, import_obsidian.requestUrl)({
      url: this.endpoint("/api/memories/people/suggestions?limit=30"),
      method: "GET",
      headers: { Authorization: `Bearer ${this.apiKey()}` }
    });
    return response.json.suggestions;
  }
  async markSeparate(suggestion) {
    await (0, import_obsidian.requestUrl)({
      url: this.endpoint("/api/memories/people/identity"),
      method: "POST",
      headers: {
        Authorization: `Bearer ${this.apiKey()}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        person_a: suggestion.person_a.name,
        person_b: suggestion.person_b.name,
        decision: "distinct",
        revision: suggestion.revision
      })
    });
  }
  async dismissSuggestion(suggestion) {
    const dismissal = `${suggestion.pair_id}:${suggestion.revision}`;
    this.settings.dismissedSuggestions = [
      .../* @__PURE__ */ new Set([...this.settings.dismissedSuggestions, dismissal])
    ].slice(-500);
    await this.saveSettings();
  }
  async reviewDuplicatePeople() {
    new import_obsidian.Notice("Looking for possible duplicate people\u2026");
    try {
      const suggestions = await this.getSuggestions();
      const dismissed = new Set(this.settings.dismissedSuggestions);
      const pending = suggestions.filter(
        (suggestion) => !dismissed.has(`${suggestion.pair_id}:${suggestion.revision}`)
      );
      if (!pending.length) {
        new import_obsidian.Notice("No new possible duplicate people found.");
        return;
      }
      new DuplicateReviewModal(this, pending).open();
    } catch (error) {
      new import_obsidian.Notice(`Could not load suggestions: ${errorMessage(error)}`, 8e3);
    }
  }
};
var DuplicateReviewModal = class extends import_obsidian.Modal {
  constructor(plugin, suggestions) {
    super(plugin.app);
    this.index = 0;
    this.acting = false;
    this.plugin = plugin;
    this.suggestions = suggestions;
  }
  onOpen() {
    this.render();
  }
  render() {
    this.contentEl.empty();
    const suggestion = this.suggestions[this.index];
    if (!suggestion) {
      this.close();
      new import_obsidian.Notice("Duplicate review complete.");
      return;
    }
    this.setTitle("Possible duplicate people");
    const header = this.contentEl.createDiv({ cls: "chronicle-identity-header" });
    header.createEl("p", {
      cls: "chronicle-merge-help",
      text: `Suggestion ${this.index + 1} of ${this.suggestions.length} \xB7 confidence ${suggestion.score}`
    });
    const navigation = header.createDiv({ cls: "chronicle-identity-navigation" });
    new import_obsidian.ButtonComponent(navigation).setButtonText("Back").setDisabled(this.index === 0).onClick(() => this.move(-1));
    new import_obsidian.ButtonComponent(navigation).setButtonText("Next").setDisabled(this.index === this.suggestions.length - 1).onClick(() => this.move(1));
    const comparison = this.contentEl.createDiv({
      cls: "chronicle-identity-comparison"
    });
    this.addPerson(comparison, suggestion.person_a);
    this.addPerson(comparison, suggestion.person_b);
    const evidence = this.contentEl.createDiv({ cls: "chronicle-identity-evidence" });
    evidence.createDiv({
      cls: "chronicle-identity-evidence-title",
      text: "Why Chronicle suggested this"
    });
    const reasons = evidence.createEl("ul");
    for (const reason of suggestion.reasons) reasons.createEl("li", { text: reason });
    this.contentEl.createEl("p", {
      cls: "chronicle-merge-help",
      text: "Separate people is a durable vault annotation. Not sure hides only this version of the suggestion."
    });
    const actions = this.contentEl.createDiv({ cls: "chronicle-identity-actions" });
    new import_obsidian.ButtonComponent(actions).setButtonText("Not sure").onClick(() => void this.dismiss());
    new import_obsidian.ButtonComponent(actions).setButtonText("Separate people").onClick(() => void this.separate());
    new import_obsidian.ButtonComponent(actions).setButtonText("Same person\u2026").setCta().onClick(() => {
      this.close();
      new CanonicalPersonModal(this.plugin, suggestion).open();
    });
  }
  move(offset) {
    const nextIndex = this.index + offset;
    if (nextIndex < 0 || nextIndex >= this.suggestions.length) return;
    this.index = nextIndex;
    this.render();
  }
  removeCurrent() {
    this.suggestions.splice(this.index, 1);
    if (this.index >= this.suggestions.length) this.index = this.suggestions.length - 1;
    this.render();
  }
  addPerson(parent, person) {
    const card = parent.createDiv({ cls: "chronicle-identity-person" });
    card.createEl("h3", { text: person.name });
    card.createEl("div", { cls: "chronicle-merge-path", text: person.path });
    if (person.snippets.length) {
      const snippets = card.createEl("ul");
      for (const snippet of person.snippets.slice(0, 3)) {
        snippets.createEl("li", { text: snippet });
      }
    }
    const open = card.createEl("button", { text: "Open note" });
    open.addEventListener("click", () => void this.openNote(person.path));
  }
  async openNote(path) {
    const file = this.app.vault.getAbstractFileByPath(path);
    if (!(file instanceof import_obsidian.TFile)) {
      new import_obsidian.Notice(`Could not find ${path}. The vault may still be syncing.`);
      return;
    }
    await this.app.workspace.getLeaf("tab").openFile(file);
  }
  async dismiss() {
    if (this.acting) return;
    this.acting = true;
    await this.plugin.dismissSuggestion(this.suggestions[this.index]);
    this.acting = false;
    this.removeCurrent();
  }
  async separate() {
    if (this.acting) return;
    this.acting = true;
    const suggestion = this.suggestions[this.index];
    try {
      await this.plugin.markSeparate(suggestion);
      new import_obsidian.Notice(`${suggestion.person_a.name} and ${suggestion.person_b.name} marked as separate.`);
      this.acting = false;
      this.removeCurrent();
    } catch (error) {
      this.acting = false;
      new import_obsidian.Notice(`Could not save decision: ${errorMessage(error)}`, 8e3);
    }
  }
};
var CanonicalPersonModal = class extends import_obsidian.Modal {
  constructor(plugin, suggestion) {
    super(plugin.app);
    this.plugin = plugin;
    this.suggestion = suggestion;
  }
  onOpen() {
    const { person_a: personA, person_b: personB } = this.suggestion;
    this.setTitle("Which name should Chronicle keep?");
    this.contentEl.createEl("p", {
      cls: "chronicle-merge-help",
      text: "The other name is retained as an alias, and links are rewritten after a final preview."
    });
    const actions = this.contentEl.createDiv({ cls: "chronicle-canonical-actions" });
    new import_obsidian.ButtonComponent(actions).setButtonText(`Keep ${personA.name}`).setCta().onClick(() => void this.choose(personB, personA));
    new import_obsidian.ButtonComponent(actions).setButtonText(`Keep ${personB.name}`).setCta().onClick(() => void this.choose(personA, personB));
  }
  async choose(sourcePerson, targetPerson) {
    const source = this.app.vault.getAbstractFileByPath(sourcePerson.path);
    const target = this.app.vault.getAbstractFileByPath(targetPerson.path);
    if (!(source instanceof import_obsidian.TFile) || !(target instanceof import_obsidian.TFile)) {
      new import_obsidian.Notice("One of the person notes is not available yet. Wait for vault sync and retry.");
      return;
    }
    this.close();
    new import_obsidian.Notice("Checking that the Chronicle vault is in sync\u2026");
    try {
      const preview = await this.plugin.previewMerge(source, target);
      new MergePreviewModal(this.plugin, preview).open();
    } catch (error) {
      new import_obsidian.Notice(`Could not preview merge: ${errorMessage(error)}`, 8e3);
    }
  }
};
var PersonPickerModal = class extends import_obsidian.Modal {
  constructor(plugin, source) {
    super(plugin.app);
    this.people = [];
    this.plugin = plugin;
    this.source = source;
  }
  onOpen() {
    this.setTitle(`Merge ${this.source.basename} into\u2026`);
    this.people = this.app.vault.getMarkdownFiles().filter((file) => personName(file) && file.path !== this.source.path).sort((left, right) => left.basename.localeCompare(right.basename));
    const search = new import_obsidian.SearchComponent(this.contentEl);
    search.setPlaceholder("Find the canonical person");
    search.inputEl.setAttr("aria-label", "Find the canonical person");
    search.onChange((query) => this.renderPeople(query));
    this.listEl = this.contentEl.createDiv({ cls: "chronicle-merge-list" });
    this.renderPeople("");
    search.inputEl.focus();
  }
  renderPeople(query) {
    this.listEl.empty();
    const normalized = query.trim().toLocaleLowerCase();
    const matches = this.people.filter(
      (file) => file.basename.toLocaleLowerCase().includes(normalized)
    );
    for (const file of matches) {
      const button = this.listEl.createEl("button", {
        cls: "chronicle-merge-person clickable-icon",
        text: file.basename
      });
      button.setAttr("aria-label", `Merge ${this.source.basename} into ${file.basename}`);
      button.addEventListener("click", () => void this.choose(file));
    }
    if (!matches.length) {
      this.listEl.createDiv({ cls: "chronicle-merge-help", text: "No matching people." });
    }
  }
  async choose(target) {
    this.close();
    new import_obsidian.Notice("Checking that the Chronicle vault is in sync\u2026");
    try {
      const preview = await this.plugin.previewMerge(this.source, target);
      new MergePreviewModal(this.plugin, preview).open();
    } catch (error) {
      new import_obsidian.Notice(`Could not preview merge: ${errorMessage(error)}`, 8e3);
    }
  }
};
var MergePreviewModal = class extends import_obsidian.Modal {
  constructor(plugin, preview) {
    super(plugin.app);
    this.applying = false;
    this.plugin = plugin;
    this.preview = preview;
  }
  onOpen() {
    this.setTitle(`Merge ${this.preview.source_name} into ${this.preview.target_name}?`);
    this.contentEl.createEl("p", {
      cls: "chronicle-merge-path",
      text: `${this.preview.source_path} \u2192 ${this.preview.target_path}`
    });
    const summary = this.contentEl.createDiv({ cls: "chronicle-merge-summary" });
    this.addStat(summary, String(this.preview.facts_to_add), "facts added");
    this.addStat(summary, String(this.preview.backlink_occurrences), "links rewritten");
    this.addStat(summary, String(this.preview.backlink_files.length), "notes updated");
    this.addStat(
      summary,
      String(this.preview.duplicate_facts_skipped),
      "duplicates skipped"
    );
    if (this.preview.metadata_conflicts.length) {
      this.contentEl.createEl("p", {
        text: "The canonical note keeps these existing values:"
      });
      const conflicts = this.contentEl.createEl("ul", { cls: "chronicle-merge-conflicts" });
      for (const conflict of this.preview.metadata_conflicts) {
        conflicts.createEl("li", {
          text: `${conflict.field}: ${String(conflict.target_value)} (discarding ${String(conflict.source_value)})`
        });
      }
    }
    this.contentEl.createEl("p", {
      cls: "chronicle-merge-help",
      text: `\u201C${this.preview.source_name}\u201D will be retained as an alias. Chronicle will audit every changed note.`
    });
    const actions = this.contentEl.createDiv({ cls: "chronicle-merge-actions" });
    new import_obsidian.ButtonComponent(actions).setButtonText("Cancel").onClick(() => this.close());
    new import_obsidian.ButtonComponent(actions).setButtonText("Merge person").setWarning().onClick(() => void this.apply());
  }
  addStat(parent, value, label) {
    const stat = parent.createDiv({ cls: "chronicle-merge-stat" });
    stat.createSpan({ cls: "chronicle-merge-stat-value", text: value });
    stat.createSpan({ cls: "chronicle-merge-stat-label", text: label });
  }
  async apply() {
    if (this.applying) return;
    this.applying = true;
    try {
      await this.plugin.applyMerge(this.preview);
      this.close();
      new import_obsidian.Notice(
        `Merged ${this.preview.source_name} into ${this.preview.target_name}. Waiting for vault sync.`,
        8e3
      );
    } catch (error) {
      this.applying = false;
      new import_obsidian.Notice(`Merge failed: ${errorMessage(error)}`, 8e3);
    }
  }
};
var ChronicleSettingTab = class extends import_obsidian.PluginSettingTab {
  constructor(app, plugin) {
    super(app, plugin);
    this.plugin = plugin;
  }
  display() {
    this.containerEl.empty();
    new import_obsidian.Setting(this.containerEl).setName("Chronicle server").setDesc("HTTPS address reachable from this device").addText(
      (text) => text.setPlaceholder("https://chronicle.example.com").setValue(this.plugin.settings.baseUrl).onChange(async (value) => {
        this.plugin.settings.baseUrl = value.trim();
        await this.plugin.saveSettings();
      })
    );
    new import_obsidian.Setting(this.containerEl).setName("Chronicle API key").setDesc("Select or create a long-lived Chronicle API key in Obsidian SecretStorage").addComponent(
      (element) => new import_obsidian.SecretComponent(this.app, element).setValue(this.plugin.settings.secretName).onChange(async (value) => {
        this.plugin.settings.secretName = value;
        await this.plugin.saveSettings();
      })
    );
  }
};
function errorMessage(error) {
  return error instanceof Error ? error.message : String(error);
}
