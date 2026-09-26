// Synthetic corner treatments: identical graph, palette and typography.
export const shapeSource = `graph LR
%% tin:composition
subgraph work["Work and review"]
  direction TD
  %% tin:group frame
  subgraph first
    direction LR
    %% tin:group layout
    ask["Prepare draft"]:::step
    run["Workspace"]:::surface
    review["Human review"]:::gate
  end
  subgraph second
    direction LR
    %% tin:group layout
    saved[("Saved draft")]:::store
    proof["Save confirmed"]:::receipt
    wait["Wait for launch"]:::wait
    cancelled["Cancelled launch"]:::ghost
  end
end
ask --> run
run --> review
review -->|approved| saved
saved --> proof
proof --> wait
wait -.->|cancelled| cancelled
`;

export const brandShapeFixtures = [undefined, "sharp", "soft", "round"].map(shape => ({
  id: `corners-${shape || "default"}`, title: `${shape || "Default"} corners`,
  source: shapeSource.replace("graph LR\n", `graph LR\n%% tin:brand ${JSON.stringify({
    revision: "a".repeat(40), sha256: "b".repeat(64),
    light: {ink: "#18242C", paper: "#FAFAF5", accent: "#2265BD"},
    ...(shape ? {shape} : {}),
  })}\n`),
}));
