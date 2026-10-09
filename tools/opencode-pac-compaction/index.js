/** Native OpenCode checkpoint requests are text summaries, not tool turns. */
export const COMPACTION_SYSTEM = "Summarize only the factual conversation. Follow the checkpoint format appended after the transcript. Do not call tools.";

export default {
  id: "pac.compaction",
  async setup(ctx) {
    const registration = await ctx.session.hook("compaction", (event) => {
      if (event.model.providerID !== "pac") return;
      // OpenCode appends its own summary template after this supported hook.
      // Keep its transcript/result/model/options and native compaction intact.
      event.tools = {};
      event.system = [{ text: COMPACTION_SYSTEM }];
    }, { providerID: "pac" });
    return () => registration.dispose();
  },
};
