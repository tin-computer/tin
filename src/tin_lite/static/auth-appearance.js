"use strict";

(() => {
// Shared Paper 25–27 styling for Tin identity and connection screens.
const TIN_AUTH_APPEARANCE = {
  options: {
    socialButtonsPlacement: "top",
    socialButtonsVariant: "blockButton",
    termsPageUrl: "https://tin.computer/legal/terms",
    privacyPageUrl: "https://tin.computer/legal/privacy",
  },
  variables: {
    colorScheme: document.documentElement.dataset.theme === "dark" ? "dark" : "light",
    colorPrimary: "var(--hot)",
    colorForeground: "var(--ink)",
    colorBackground: "var(--paper-card)",
    colorInput: "var(--input-bg)",
    colorInputForeground: "var(--ink)",
    colorMutedForeground: "var(--ink-muted)",
    colorNeutral: "var(--ink)",
    borderRadius: "4px",
    fontFamily: "var(--sans)",
    fontFamilyButtons: "var(--sans)",
  },
  elements: {
    rootBox: { width: "100%", overflow: "visible" },
    cardBox: {
      width: "100%",
      overflow: "visible",
      boxShadow: "none",
    },
    card: {
      width: "100%",
      padding: 0,
      gap: "18px",
      overflow: "visible",
      background: "transparent",
      border: "none",
      borderRadius: 0,
      boxShadow: "none",
    },
    logoBox: { display: "none" },
    header: {
      alignItems: "flex-start",
      textAlign: "left",
    },
    headerTitle: {
      color: "var(--ink)",
      fontSize: "40px",
      fontWeight: 400,
      letterSpacing: "-0.04em",
      lineHeight: "46px",
    },
    headerSubtitle: {
      marginTop: "10px",
      color: "var(--ink-secondary)",
      fontSize: "15px",
      lineHeight: "22px",
      textAlign: "left",
    },
    main: { gap: "18px", overflow: "visible" },
    socialButtonsRoot: { marginTop: 0, overflow: "visible" },
    socialButtonsBlockButton: {
      height: "46px",
      color: "var(--ink)",
      background: "var(--paper-card)",
      border: "1px solid rgb(var(--ink-rgb) / 25%)",
      borderRadius: "999px",
      boxShadow: "none",
      fontSize: "14px",
      fontWeight: 400,
      transition: "border-color 120ms ease, background-color 120ms ease",
      "&:hover, &:focus": {
        color: "var(--ink)",
        background: "var(--paper-bg)",
        borderColor: "rgb(var(--ink-rgb) / 45%)",
      },
    },
    socialButtonsBlockButtonText: {
      fontSize: "14px",
      fontWeight: 400,
    },
    lastAuthenticationStrategyBadge: {
      top: "50%",
      insetInlineEnd: "10px",
      height: "20px",
      paddingInline: "7px",
      transform: "translateY(-50%)",
      color: "var(--ink-muted)",
      background: "var(--paper-well)",
      border: "1px solid rgb(var(--ink-rgb) / 12%)",
      borderRadius: "999px",
      boxShadow: "none",
      fontFamily: "var(--mono)",
      fontSize: "10px",
      lineHeight: "18px",
    },
    dividerRow: {
      gap: "12px",
      margin: 0,
    },
    dividerLine: { background: "rgb(var(--ink-rgb) / 12%)" },
    dividerText: {
      color: "var(--ink-muted)",
      fontFamily: "var(--mono)",
      fontSize: "11px",
      lineHeight: "14px",
    },
    formFieldLabel: { display: "none" },
    formFieldInput: {
      height: "46px",
      padding: "0 14px",
      color: "var(--ink)",
      background: "var(--input-bg)",
      border: "1px solid rgb(var(--ink-rgb) / 25%)",
      borderRadius: "4px",
      boxShadow: "none",
      fontFamily: "var(--mono)",
      fontSize: "13px",
      lineHeight: "16px",
      "&:focus": {
        borderColor: "var(--accent-deep)",
        boxShadow: "0 0 0 1px var(--accent-deep)",
      },
    },
    formButtonPrimary: {
      height: "46px",
      marginTop: "10px",
      color: "var(--on-hot)",
      background: "var(--hot)",
      borderRadius: "999px",
      boxShadow: "none",
      fontSize: "14px",
      fontWeight: 700,
      textTransform: "none",
      transition: "background-color 120ms ease",
      "&:hover, &:focus": { background: "var(--hot-hover)" },
      "&:active": { background: "var(--hot-pressed)" },
    },
    // Clerk pulls the footer up 8px and pads every footer row; only nested
    // selectors beat those defaults. The "Secured by" row is the last child on
    // every step (and the only child on the code step), so it always carries
    // the 16px gap itself and never rides up into the row above.
    footer: {
      margin: 0,
      padding: 0,
      background: "transparent",
      alignItems: "stretch",
      "> :not(:last-child)": { padding: 0 },
      "> :last-child": { padding: "16px 0 0" },
    },
    footerAction: {
      width: "100%",
      justifyContent: "flex-start",
      gap: "5px",
      margin: "44px 0 16px",
      padding: 0,
      background: "transparent",
    },
    footerActionText: {
      color: "var(--ink-secondary)",
      fontSize: "13px",
      lineHeight: "16px",
    },
    footerActionLink: {
      color: "var(--hot)",
      fontSize: "13px",
      fontWeight: 700,
      lineHeight: "16px",
      transition: "color 120ms ease",
      "&:hover, &:focus": { color: "var(--hot-hover)" },
      "&:active": { color: "var(--hot-pressed)" },
    },
    footerItem: {
      width: "100%",
      "& > div": {
        marginInline: 0,
        alignItems: "flex-start",
      },
      "& > div > div": { justifyContent: "flex-start" },
    },
    footerPages: { display: "none" },
    identityPreview: {
      width: "100%",
      marginTop: "4px",
      padding: 0,
      justifyContent: "flex-start",
      background: "transparent",
      border: 0,
    },
    identityPreviewText: {
      color: "var(--ink)",
      fontFamily: "var(--mono)",
      fontSize: "13px",
    },
    otpCodeField: { alignSelf: "flex-start" },
    otpCodeFieldInputContainer: { alignSelf: "flex-start" },
    otpCodeFieldInputs: { gap: "8px", justifyContent: "flex-start" },
    otpCodeFieldInput: {
      width: "48px",
      height: "56px",
      color: "var(--ink)",
      background: "var(--input-bg)",
      border: "1px solid rgb(var(--ink-rgb) / 25%)",
      borderRadius: "4px",
      boxShadow: "none",
      fontFamily: "var(--mono)",
      fontSize: "20px",
      lineHeight: "24px",
    },
    formResendCodeLink: {
      alignSelf: "flex-start",
      marginTop: "8px",
      color: "var(--accent-text)",
      fontSize: "12.5px",
      fontWeight: 700,
      lineHeight: "16px",
      textAlign: "left",
    },
  },
};

const TIN_AUTH_LOCALIZATION = {
  formFieldInputPlaceholder__emailAddress: "you@company.com",
  formFieldInputPlaceholder__emailAddress_username: "you@company.com",
  signIn: {
    start: {
      title: "Welcome back.",
      titleCombined: "Welcome back.",
      subtitle: "Sign in to see what ran while you were away.",
      subtitleCombined: "Sign in to see what ran while you were away.",
      actionText: "New to Tin?",
      actionLink: "Create an account",
    },
    emailCode: {
      title: "Check your inbox.",
      subtitle: "A six-digit code went to your email.",
      formTitle: "Verification code",
      resendButton: "Resend code",
    },
  },
  signUp: {
    start: {
      title: "Create your account.",
      titleCombined: "Create your account.",
      subtitle: "Start with a scan of your site. Your agent takes it from there.",
      subtitleCombined: "Start with a scan of your site. Your agent takes it from there.",
      actionText: "Already using Tin?",
      actionLink: "Sign in",
    },
    emailCode: {
      title: "Check your inbox.",
      subtitle: "A six-digit code went to your email.",
      formTitle: "Verification code",
      formSubtitle: "Enter the six-digit code.",
      resendButton: "Resend code",
    },
  },
  // Clerk's defaults ("That email address is taken", "Couldn't find your account") read
  // as a wall to someone who signed up before, often on tin.computer, which shares this
  // Clerk instance. Say which form they need instead; its link sits under the form.
  unstable__errors: {
    form_identifier_exists__email_address:
      "You already have a Tin account with this email. Sign in below instead.",
    form_identifier_not_found:
      "No Tin account uses this email yet. Check the spelling, or create an account below.",
  },
};

window.TinAuth = { appearance: TIN_AUTH_APPEARANCE, localization: TIN_AUTH_LOCALIZATION };
})();
