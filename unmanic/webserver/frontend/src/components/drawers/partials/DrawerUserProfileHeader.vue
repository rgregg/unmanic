<template>
  <!-- Local fork: avatar/name (sourced from api.unmanic.app via
       fetch_user_data, now stubbed) and "Support future development"
       button (links to api.unmanic.app/support-auth-api/support) both
       removed. Keep just the gradient header for visual continuity. -->
  <div class="drawer-profile">
    <q-img
      src="~assets/bg-design-3.png"
      style="height: 90px">
      <div class="absolute-full profile-gradient"></div>
    </q-img>
  </div>
</template>

<script>
export default {
  name: 'DrawerUserProfileHeader',
  setup() {
    return {}
  }
}
</script>

<style>

.support-btn-img {
  display: initial !important;
  content: url('~assets/coffee-btn-image.png')
}

.profile-gradient {
  background: linear-gradient(to bottom, #002e5c, rgba(0, 46, 92, 0.7)) !important;
}

.support-btn-img {
  display: initial;
  animation: coffee-wiggle 13s infinite;
}

@keyframes coffee-wiggle {
  0% {
    transform: rotate(0) scale(1)
  }
  0.5%, 1.5%, 2.5%, 3.5%, 4.5% {
    transform: translate3d(-2px, 0, 0);
  }
  1%, 2%, 3%, 4% {
    transform: translate3d(2px, 0, 0);
  }
  5% {
    transform: translate3d(0px, 0, 0);
  }
  14% {
    transform: rotate(0) scale(1);
  }
  17% {
    transform: rotate(0) scale(1.12);
  }
  18.5% {
    transform: rotate(0) scale(1.1);
  }
  19.5% {
    transform: rotate(-10deg) scale(1.3);
  }
  20.5% {
    transform: rotate(10deg) scale(1.3);
  }
  21.5% {
    transform: rotate(-10deg) scale(1.3);
  }
  22.5% {
    transform: rotate(10deg) scale(1.3);
  }
  23%, 100% {
    transform: rotate(0) scale(1);
  }
}

</style>
