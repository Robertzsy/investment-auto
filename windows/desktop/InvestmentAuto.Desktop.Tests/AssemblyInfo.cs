using Xunit;

// Several tests touch process-wide state (PATH, registry, named mutex);
// keep the whole assembly sequential for determinism.
[assembly: CollectionBehavior(DisableTestParallelization = true)]
